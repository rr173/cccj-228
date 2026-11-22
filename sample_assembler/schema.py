# -*- coding: utf-8 -*-
"""SQLite 持久化 schema。

设计要点：
- 所有占用（assignment/gap）都落库，进程重开后状态不丢失，替补不会被重复占用。
- ``ux_assignment_substitute_frame`` 部分唯一索引在数据库层保证同一 frame 单位
  最多被一次替补占用（跨进程/跨连接同样生效），配合重试消除双重分配。
- 原始抽中记录（kind='original'）不参与该唯一约束，允许失败批次回滚后重建。
- 资格事件以 (frame_id, event_type, event_time, source_seq) 去重，实现幂等。
"""

SCHEMA_VERSION = 1

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
]


def initialize_db(conn):
    """建表并写入 schema 版本（幂等）。"""
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
    elif int(row[0]) != SCHEMA_VERSION:
        raise RuntimeError(
            "schema 版本不匹配：库=%s 代码=%s" % (row[0], SCHEMA_VERSION)
        )
