# -*- coding: utf-8 -*-
"""SQLite 持久化 schema。

设计要点：
- 所有占用（assignment/gap）都落库，进程重开后状态不丢失，替补不会被重复占用。
- ``ux_assignment_substitute_frame`` 部分唯一索引在数据库层保证同一 frame 单位
  最多被一次替补占用（跨进程/跨连接同样生效），配合重试消除双重分配。
- 原始抽中记录（kind='original'）不参与该唯一约束，允许失败批次回滚后重建。
- 资格事件以 (frame_id, event_type, event_time, source_seq) 去重，实现幂等。
"""

SCHEMA_VERSION = 4

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # ---- 规则集 -------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS rule_set (
        rule_set_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL UNIQUE,
        is_active     INTEGER NOT NULL DEFAULT 0,
        compiled_at   TEXT NOT NULL,
        missing_stratum TEXT,
        extra_json    TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stratum_rule (
        rule_set_id   INTEGER NOT NULL REFERENCES rule_set(rule_set_id),
        stratum_code  TEXT NOT NULL,
        ordinal       INTEGER NOT NULL,
        target_n      INTEGER NOT NULL,
        capacity      INTEGER NOT NULL,
        kind          TEXT NOT NULL,            -- categorical / numeric
        raw_json      TEXT NOT NULL,
        PRIMARY KEY (rule_set_id, stratum_code),
        UNIQUE (rule_set_id, ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rule_condition (
        rule_set_id   INTEGER NOT NULL,
        stratum_code  TEXT NOT NULL,
        cond_index    INTEGER NOT NULL,
        kind          TEXT NOT NULL,            -- value / range
        value         TEXT,
        lower_inclusive REAL,
        upper_exclusive REAL,
        PRIMARY KEY (rule_set_id, stratum_code, cond_index),
        FOREIGN KEY (rule_set_id, stratum_code)
            REFERENCES stratum_rule(rule_set_id, stratum_code)
    )
    """,
    # ---- 名单 ---------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS frame_unit (
        frame_id        TEXT PRIMARY KEY,
        frame_version   TEXT NOT NULL,
        business_unit_id TEXT,
        stratum_value   TEXT,                   -- 原始层字段，NULL 表示缺失
        is_missing      INTEGER NOT NULL,       -- 1=层字段缺失
        contactable     INTEGER NOT NULL,
        random_rank     REAL NOT NULL,
        inclusion_prob  REAL,
        imported_at     TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_frame_version_stratum
        ON frame_unit(frame_version, stratum_value)
    """,
    """
    CREATE TABLE IF NOT EXISTS identity_map (
        business_unit_id TEXT NOT NULL,
        frame_id         TEXT NOT NULL,
        frame_version    TEXT NOT NULL,
        PRIMARY KEY (business_unit_id, frame_id)
    )
    """,
    # ---- 原始抽样结果 -------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS sample_draw (
        sample_id       TEXT PRIMARY KEY,
        frame_id        TEXT NOT NULL,
        frame_version   TEXT NOT NULL,
        target_stratum  TEXT NOT NULL,
        random_rank     REAL NOT NULL,
        selection_position INTEGER NOT NULL,
        inclusion_prob  REAL,
        UNIQUE (frame_version, target_stratum, selection_position)
    )
    """,
    # ---- 资格事件（原始审计流） ---------------------------------------
    """
    CREATE TABLE IF NOT EXISTS eligibility_event (
        event_id      TEXT PRIMARY KEY,
        frame_id      TEXT NOT NULL,
        event_type    TEXT NOT NULL,           -- lost/refused/eligible/withdrew
        event_time    TEXT NOT NULL,
        source_seq    INTEGER NOT NULL,
        accepted      INTEGER NOT NULL,        -- 1 接受并驱动状态；0 逆序/非法被拒
        reject_reason TEXT,
        recorded_at   TEXT NOT NULL,
        UNIQUE (frame_id, event_type, event_time, source_seq)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_event_frame ON eligibility_event(frame_id)
    """,
    # 单位当前确认状态（只由 accepted 事件推进）
    """
    CREATE TABLE IF NOT EXISTS unit_status (
        frame_id        TEXT PRIMARY KEY,
        current_state   TEXT NOT NULL,         -- pending/lost/refused/eligible/withdrew
        last_event_time TEXT,
        last_source_seq INTEGER,
        last_event_id   TEXT,
        FOREIGN KEY (last_event_id) REFERENCES eligibility_event(event_id)
    )
    """,
    # ---- 装配批次 / 缺口 / assignment --------------------------------
    """
    CREATE TABLE IF NOT EXISTS batch (
        batch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_set_id     INTEGER NOT NULL,
        frame_version   TEXT NOT NULL,
        status          TEXT NOT NULL,         -- draft/published/failed
        created_at      TEXT NOT NULL,
        published_at    TEXT,
        note            TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gap (
        gap_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id        INTEGER NOT NULL REFERENCES batch(batch_id),
        stratum_code    TEXT NOT NULL,
        gap_seq         INTEGER NOT NULL,
        origin_sample_id TEXT NOT NULL,        -- 最初失败的抽中记录
        trigger_event_id TEXT NOT NULL,
        trigger_frame_id TEXT NOT NULL,
        status          TEXT NOT NULL,         -- open/filled/unfillable
        filled_by_assignment_id INTEGER,
        reason          TEXT,                  -- unfillable 原因
        considered_json TEXT NOT NULL DEFAULT '[]',
        UNIQUE (batch_id, gap_seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assignment (
        assignment_id   TEXT PRIMARY KEY,
        batch_id        INTEGER NOT NULL REFERENCES batch(batch_id),
        kind            TEXT NOT NULL,         -- original/substitute
        stratum_code    TEXT NOT NULL,
        frame_id        TEXT NOT NULL,
        frame_version   TEXT NOT NULL,
        business_unit_id TEXT,
        sample_id       TEXT,                  -- original 引用；substitute 为空
        root_sample_id  TEXT NOT NULL,         -- 链根（原始抽中 sample_id）
        parent_gap_id   INTEGER,               -- 替补所填缺口
        chain_seq       INTEGER NOT NULL,      -- 0=原始，1+=替补次序
        selection_position INTEGER NOT NULL,
        random_rank     REAL NOT NULL,
        inclusion_prob  REAL NOT NULL,         -- 保留原抽样概率
        status          TEXT NOT NULL,         -- active/terminal_fail
        created_event_id TEXT,
        UNIQUE (batch_id, frame_id)
    )
    """,
    # 关键：同一 frame 单位在所有已持久化批次中至多被一次替补占用。
    # 仅约束 substitute；original 来自 sample_draw，失败批次回滚后需要可重建。
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_assignment_substitute_frame
        ON assignment(frame_id) WHERE kind = 'substitute'
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_assignment_batch ON assignment(batch_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_gap_batch ON gap(batch_id)
    """,
    # ---- 已发布版本 ---------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS published_version (
        id          INTEGER PRIMARY KEY CHECK (id = 1),
        batch_id    INTEGER NOT NULL REFERENCES batch(batch_id),
        version_no  INTEGER NOT NULL,
        published_at TEXT NOT NULL
    )
    """,
    # ---- panel 轮换：声明式轮换规则 ----------------------------------
    """
    CREATE TABLE IF NOT EXISTS rotation_rule_set (
        rotation_rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name             TEXT NOT NULL UNIQUE,
        frame_rule_set_id INTEGER NOT NULL REFERENCES rule_set(rule_set_id),
        max_consecutive_waves INTEGER NOT NULL,
        cooldown_waves   INTEGER NOT NULL,
        min_retention_ratio REAL NOT NULL,
        created_at       TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rotation_stratum_target (
        rotation_rule_id INTEGER NOT NULL
            REFERENCES rotation_rule_set(rotation_rule_id),
        stratum_code TEXT NOT NULL,
        target_n     INTEGER NOT NULL,
        ordinal      INTEGER NOT NULL,
        PRIMARY KEY (rotation_rule_id, stratum_code)
    )
    """,
    # ---- panel 成员稳定身份（锚定业务身份，跨轮次不覆盖） -------------
    """
    CREATE TABLE IF NOT EXISTS panel_member (
        panel_member_id TEXT PRIMARY KEY,     -- PM_<member_key 哈希>
        member_key      TEXT NOT NULL UNIQUE, -- business_unit_id 或 frame:<frame_id>
        anchor_business_unit_id TEXT,
        anchor_frame_id TEXT NOT NULL,        -- 首次进入 panel 时的名单行
        first_wave      INTEGER NOT NULL,     -- 首次进入的 wave（样本轮=0）
        created_at      TEXT NOT NULL
    )
    """,
    # ---- panel 计划（可重试版本；request_key 幂等） -------------------
    """
    CREATE TABLE IF NOT EXISTS panel_plan (
        plan_id         INTEGER PRIMARY KEY AUTOINCREMENT,
        request_key     TEXT NOT NULL UNIQUE,  -- 相同请求键重复计算返回同一计划
        rotation_rule_id INTEGER NOT NULL
            REFERENCES rotation_rule_set(rotation_rule_id),
        wave            INTEGER NOT NULL,
        frame_version   TEXT NOT NULL,         -- 本轮新候选名单快照
        base_plan_id    INTEGER,               -- 上一已发布计划（首轮为空）
        base_batch_id   INTEGER,               -- 首轮基于的已发布样本批次
        status          TEXT NOT NULL,         -- draft / published
        created_at      TEXT NOT NULL,
        published_at    TEXT,
        note            TEXT
    )
    """,
    # ---- 逐成员变更账本（只 INSERT，不覆盖历史轮次） ------------------
    """
    CREATE TABLE IF NOT EXISTS panel_change (
        change_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id         INTEGER NOT NULL REFERENCES panel_plan(plan_id),
        wave            INTEGER NOT NULL,
        panel_member_id TEXT NOT NULL REFERENCES panel_member(panel_member_id),
        stratum_code    TEXT NOT NULL,
        decision        TEXT NOT NULL,         -- retained / exited / joined
        reason          TEXT NOT NULL,         -- 决定原因
        frame_id        TEXT,                  -- 本轮占用（退出时为来源）名单行
        business_unit_id TEXT,
        source_assignment_id TEXT,             -- 上一轮来源 assignment 身份
        assignment_id   TEXT,                  -- 本轮 assignment 身份（exited 为空）
        consecutive_waves INTEGER NOT NULL,    -- 决定后的连续在样轮数
        cooldown_remaining INTEGER NOT NULL,   -- 决定后的冷却计数
        created_at      TEXT NOT NULL,
        UNIQUE (plan_id, panel_member_id)
    )
    """,
    # 计划内：同一名单行至多被一个成员占用（留存或新加入）。
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_panel_change_plan_frame
        ON panel_change(plan_id, frame_id)
        WHERE decision IN ('retained', 'joined')
    """,
    # 全局兜底：同一名单行至多被“新加入”占用一次（跨计划/跨进程生效，
    # 与 ux_assignment_substitute_frame 同款语义；draft 计划也构成占用）。
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_panel_change_joined_frame
        ON panel_change(frame_id) WHERE decision = 'joined'
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_panel_change_member
        ON panel_change(panel_member_id)
    """,
    # ---- 每层缺口与候选证据 -------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS panel_stratum_gap (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id      INTEGER NOT NULL REFERENCES panel_plan(plan_id),
        stratum_code TEXT NOT NULL,
        target_n     INTEGER NOT NULL,
        prev_n       INTEGER NOT NULL,         -- 上一轮该层在样数
        retained_n   INTEGER NOT NULL,
        joined_n     INTEGER NOT NULL,
        open_slots   INTEGER NOT NULL,         -- 未能补齐的名额
        retention_required INTEGER NOT NULL,   -- 最低留存要求
        retention_ok INTEGER NOT NULL,
        reason       TEXT,                     -- 不可补齐 / 留存不足说明
        considered_json TEXT NOT NULL DEFAULT '[]',
        UNIQUE (plan_id, stratum_code)
    )
    """,
    # ---- 访谈回答转移：单库单端点绑定（只读既有 panel 数据） ----------
    # 一个库只维护一份转移汇总；绑定行存在后只能以同一端点重复初始化。
    """
    CREATE TABLE IF NOT EXISTS answer_transfer_binding (
        id               INTEGER PRIMARY KEY CHECK (id = 1),
        endpoint_plan_id INTEGER NOT NULL REFERENCES panel_plan(plan_id),
        endpoint_wave    INTEGER NOT NULL,
        created_at       TEXT NOT NULL
    )
    """,
    # 端点沿 base_plan_id 回溯得到的“已发布计划链”（含端点）。只含链上计划。
    """
    CREATE TABLE IF NOT EXISTS answer_transfer_chain (
        plan_id     INTEGER PRIMARY KEY REFERENCES panel_plan(plan_id),
        wave        INTEGER NOT NULL,
        chain_index INTEGER NOT NULL,          -- 0=链上最早一轮
        base_plan_id INTEGER,
        UNIQUE (chain_index)
    )
    """,
    # 链上每个在样记录（retained/joined）的层与在样区段；层取自该计划账本。
    """
    CREATE TABLE IF NOT EXISTS answer_transfer_sample (
        plan_id         INTEGER NOT NULL
            REFERENCES answer_transfer_chain(plan_id),
        panel_member_id TEXT NOT NULL REFERENCES panel_member(panel_member_id),
        wave            INTEGER NOT NULL,
        chain_index     INTEGER NOT NULL,
        stratum_code    TEXT NOT NULL,
        segment_no      INTEGER NOT NULL,      -- 同一成员内连续在样区段编号
        PRIMARY KEY (plan_id, panel_member_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_answer_sample_member_segment
        ON answer_transfer_sample(panel_member_id, segment_no, chain_index)
    """,
    # 访谈修订原始流（只接受链上在样记录的键）：answer=NULL 表示作废。
    """
    CREATE TABLE IF NOT EXISTS answer_revision (
        plan_id         INTEGER NOT NULL,
        panel_member_id TEXT NOT NULL,
        revision        INTEGER NOT NULL CHECK (revision > 0),
        answer          INTEGER CHECK (answer IS NULL OR answer IN (0, 1)),
        recorded_at     TEXT NOT NULL,
        PRIMARY KEY (plan_id, panel_member_id, revision),
        FOREIGN KEY (plan_id, panel_member_id)
            REFERENCES answer_transfer_sample(plan_id, panel_member_id)
    )
    """,
    # 每个 (计划, 成员) 的最大修订号及其答案（答案可能为 NULL=作废）。
    """
    CREATE TABLE IF NOT EXISTS answer_effective (
        plan_id         INTEGER NOT NULL,
        panel_member_id TEXT NOT NULL,
        revision        INTEGER NOT NULL,
        answer          INTEGER,
        PRIMARY KEY (plan_id, panel_member_id),
        FOREIGN KEY (plan_id, panel_member_id)
            REFERENCES answer_transfer_sample(plan_id, panel_member_id)
    )
    """,
    # 区段内已推导的相邻有效回答配对（内部明细，用于增量更正）。
    """
    CREATE TABLE IF NOT EXISTS answer_transfer_pair (
        pair_id         INTEGER PRIMARY KEY AUTOINCREMENT,
        panel_member_id TEXT NOT NULL,
        segment_no      INTEGER NOT NULL,
        from_plan_id    INTEGER NOT NULL,
        from_wave       INTEGER NOT NULL,
        to_plan_id      INTEGER NOT NULL,
        to_wave         INTEGER NOT NULL,
        stratum_code    TEXT NOT NULL,         -- 起点记录的层
        from_answer     INTEGER NOT NULL,
        to_answer       INTEGER NOT NULL,
        FOREIGN KEY (from_plan_id, panel_member_id)
            REFERENCES answer_transfer_sample(plan_id, panel_member_id),
        FOREIGN KEY (to_plan_id, panel_member_id)
            REFERENCES answer_transfer_sample(plan_id, panel_member_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_answer_pair_member_segment
        ON answer_transfer_pair(panel_member_id, segment_no)
    """,
    # 分析员直接读取的唯一汇总表：起止轮 + 起点层 + 前后答案 -> 配对数。
    """
    CREATE TABLE IF NOT EXISTS answer_transfer_summary (
        start_wave   INTEGER NOT NULL,
        end_wave     INTEGER NOT NULL,
        stratum_code TEXT NOT NULL,
        from_answer  INTEGER NOT NULL,
        to_answer    INTEGER NOT NULL,
        pair_count   INTEGER NOT NULL CHECK (pair_count > 0),
        PRIMARY KEY (start_wave, end_wave, stratum_code, from_answer, to_answer)
    )
    """,
    # 每批录入的审计行（不含配对明细；用于确认只重推了受影响区段）。
    """
    CREATE TABLE IF NOT EXISTS answer_revision_batch (
        batch_id         INTEGER PRIMARY KEY AUTOINCREMENT,
        endpoint_plan_id INTEGER NOT NULL REFERENCES panel_plan(plan_id),
        received_count   INTEGER NOT NULL,
        unique_count     INTEGER NOT NULL,
        stored_count     INTEGER NOT NULL,
        affected_members INTEGER NOT NULL,
        affected_segments INTEGER NOT NULL,
        pair_count       INTEGER NOT NULL,
        affected_json    TEXT NOT NULL DEFAULT '[]',
        created_at       TEXT NOT NULL
    )
    """,
    # 本批新落库的修订键（审计 + 测试“只重推受影响区段”的证据）。
    """
    CREATE TABLE IF NOT EXISTS answer_revision_batch_item (
        batch_id        INTEGER NOT NULL
            REFERENCES answer_revision_batch(batch_id),
        plan_id         INTEGER NOT NULL,
        panel_member_id TEXT NOT NULL,
        revision        INTEGER NOT NULL,
        PRIMARY KEY (batch_id, plan_id, panel_member_id, revision)
    )
    """,
    # ---- 研究交付包：管理员登记的交付授权 ------------------------------
    # 按 (plan_id, panel_member_id) 显式授予/撤回；未授予视为无权。
    # 只有绑定回答转移链上的在样记录才能登记。authorized=1 才能用于交付；
    # 撤回只切换状态并留时间戳，不删除授权行（审计 + 撤回后不可恢复的旧包
    # 失效判定都依赖它）。
    """
    CREATE TABLE IF NOT EXISTS delivery_authorization (
        plan_id         INTEGER NOT NULL,
        panel_member_id TEXT NOT NULL,
        authorized      INTEGER NOT NULL DEFAULT 0,
        granted_at      TEXT,
        revoked_at      TEXT,
        updated_at      TEXT NOT NULL,
        PRIMARY KEY (plan_id, panel_member_id),
        FOREIGN KEY (plan_id, panel_member_id)
            REFERENCES answer_transfer_sample(plan_id, panel_member_id)
    )
    """,
    # 一次交付的请求键。规格指纹用于识别“同键改规格”；同键同指纹直接
    # 返回已有包（含被撤回停领的包），不重算、不改写。
    """
    CREATE TABLE IF NOT EXISTS delivery_package (
        package_id      TEXT PRIMARY KEY,          -- DP_<随机>，可对外公开
        request_key     TEXT NOT NULL UNIQUE,
        spec_fingerprint TEXT NOT NULL,
        endpoint_plan_id INTEGER NOT NULL
            REFERENCES panel_plan(plan_id),
        min_members     INTEGER NOT NULL CHECK (min_members >= 2),
        plan_chain_json TEXT NOT NULL,             -- 选定计划按链顺序
        group_tree_json TEXT NOT NULL,             -- 收到规格中的分组树
        created_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS delivery_package_plan (
        package_id  TEXT NOT NULL REFERENCES delivery_package(package_id),
        plan_id     INTEGER NOT NULL REFERENCES panel_plan(plan_id),
        chain_index INTEGER NOT NULL,              -- 选定计划在绑定链上的位置
        PRIMARY KEY (package_id, plan_id),
        UNIQUE (package_id, chain_index)
    )
    """,
    # 包内交付成员的答卷：代号 + 公开组名 + 每个选定计划的二值答案。
    # member_ref 只用于内部撤回比对，领取接口不读取、不返回。
    """
    CREATE TABLE IF NOT EXISTS delivery_member (
        package_id      TEXT NOT NULL
            REFERENCES delivery_package(package_id),
        member_ref      TEXT NOT NULL,             -- 内部：panel_member_id
        code            TEXT NOT NULL,             -- 包内代号（对外）
        group_name      TEXT NOT NULL,             -- 公开组名（对外）
        group_depth     INTEGER NOT NULL,          -- 公开组深度（根=0）
        answers_json    TEXT NOT NULL,             -- 按选定计划顺序的 0/1
        PRIMARY KEY (package_id, member_ref),
        UNIQUE (package_id, code)
    )
    """,
    # 某个交付成员在某个选定计划上的授权后来被撤回 -> 整个包永久停领。
    # 与撤回在同一 BEGIN IMMEDIATE 事务内写入，领取端只看这张表，不存在
    # “已撤回但仍可领取”的可见中间状态。
    """
    CREATE TABLE IF NOT EXISTS delivery_revocation (
        package_id      TEXT NOT NULL
            REFERENCES delivery_package(package_id),
        plan_id         INTEGER NOT NULL,
        panel_member_id TEXT NOT NULL,
        revoked_at      TEXT NOT NULL,
        PRIMARY KEY (package_id, plan_id, panel_member_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_delivery_revocation_package
        ON delivery_revocation(package_id)
    """,
]


def initialize_db(conn):
    """建表并写入 schema 版本（幂等；旧库按 IF NOT EXISTS 语句平滑升级）。"""
    for stmt in SCHEMA_STATEMENTS:
        conn.execute(stmt)
    cur = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    elif int(row[0]) < SCHEMA_VERSION:
        # 所有建表语句均幂等，旧库直接补齐新表后提升版本号
        conn.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    elif int(row[0]) > SCHEMA_VERSION:
        raise RuntimeError(
            "schema 版本不匹配：库=%s 代码=%s" % (row[0], SCHEMA_VERSION)
        )
