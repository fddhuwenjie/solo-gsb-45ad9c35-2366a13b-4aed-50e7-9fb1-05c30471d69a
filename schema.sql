-- 陶瓷合窑装载确认台：数据库结构
-- 已确认记录（confirmed_loads / confirmed_load_items）只增不改，无 UPDATE/DELETE 路径。

PRAGMA journal_mode = WAL;

-- 待烧批次：登记编号与允许的峰温、保温分钟闭区间
CREATE TABLE IF NOT EXISTS batches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT NOT NULL UNIQUE,
    peak_min     REAL NOT NULL,
    peak_max     REAL NOT NULL,
    hold_min     INTEGER NOT NULL,
    hold_max     INTEGER NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (peak_min <= peak_max),
    CHECK (hold_min <= hold_max),
    CHECK (hold_min >= 0 AND hold_max >= 0)
);

-- 窑炉
CREATE TABLE IF NOT EXISTS kilns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL
);

-- 固定编号窑位（每台窑的窑位编号固定）
CREATE TABLE IF NOT EXISTS kiln_positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kiln_id     INTEGER NOT NULL REFERENCES kilns(id),
    pos_no      INTEGER NOT NULL,
    UNIQUE (kiln_id, pos_no)
);

-- 装窑草案（一台草案对应一台窑）
CREATE TABLE IF NOT EXISTS drafts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    kiln_id      INTEGER NOT NULL REFERENCES kilns(id),
    version      INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'confirmed')),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 草案占位：一批占一位
CREATE TABLE IF NOT EXISTS draft_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id     INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    batch_id     INTEGER NOT NULL REFERENCES batches(id),
    pos_no       INTEGER NOT NULL,
    UNIQUE (draft_id, batch_id),
    UNIQUE (draft_id, pos_no)
);

-- 已确认窑次（不可变装载清单头）
CREATE TABLE IF NOT EXISTS confirmed_loads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id       INTEGER NOT NULL UNIQUE REFERENCES drafts(id),
    kiln_id        INTEGER NOT NULL REFERENCES kilns(id),
    name           TEXT NOT NULL,
    peak_min       REAL NOT NULL,
    peak_max       REAL NOT NULL,
    hold_min       INTEGER NOT NULL,
    hold_max       INTEGER NOT NULL,
    confirmed_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 已确认装载清单明细（不可变快照）
-- UNIQUE(batch_id)：一个批次最多被一个已确认窑次使用；
--                   两个草案同时争用同一批次时，只有一个确认事务能提交。
CREATE TABLE IF NOT EXISTS confirmed_load_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    load_id        INTEGER NOT NULL REFERENCES confirmed_loads(id),
    batch_id       INTEGER NOT NULL UNIQUE,
    batch_code     TEXT NOT NULL,
    pos_no         INTEGER NOT NULL,
    peak_min       REAL NOT NULL,
    peak_max       REAL NOT NULL,
    hold_min       INTEGER NOT NULL,
    hold_max       INTEGER NOT NULL,
    UNIQUE (load_id, pos_no)
);
