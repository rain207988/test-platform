-- test-platform 数据库结构（SQLite）
-- 六张表：users / projects / cases / executions / execution_items / import_logs
--
-- 两条贯穿全库的设计决策：
-- 1. "执行记录"与"用例定义"彻底分离：executions/execution_items 记录的是
--    "某一次跑批发生的事实"，用例后来改了也不影响历史记录——
--    所以 execution_items 里冗余存 case_name（快照），且 case_id 不设外键
--    （用例被删后历史明细仍要能完整展示）；
-- 2. 结构不定形的字段（headers/params/body/expect_fields/断言明细）一律存 JSON 文本。
--    SQLite 没有 JSON 类型，读出时 json.loads。为什么不拆成关系列？见 docs/02。

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,           -- pbkdf2 摘要（带随机盐），存十六进制，绝不存明文
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    description TEXT    NOT NULL DEFAULT '',
    base_url    TEXT    NOT NULL DEFAULT '',  -- 该项目用例默认被测环境，如 http://127.0.0.1:5000
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS cases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name          TEXT    NOT NULL,
    method        TEXT    NOT NULL DEFAULT 'GET',
    path          TEXT    NOT NULL DEFAULT '/',
    headers       TEXT    NOT NULL DEFAULT '',  -- JSON 文本：自定义请求头，空串=无
    params        TEXT    NOT NULL DEFAULT '',  -- JSON 文本：查询串，空串=无
    body          TEXT    NOT NULL DEFAULT '',  -- JSON 文本：请求体，空串=无
    auth_user     TEXT    NOT NULL DEFAULT '',  -- 非空 = 执行前自动登录缓存 token
    auth_pass     TEXT    NOT NULL DEFAULT '',
    expect_status INTEGER,                      -- NULL = 该层不断言（三层断言每层独立可选）
    expect_code   INTEGER,                      -- 注意：0 是合法业务码，判空必须用 IS NULL
    expect_fields TEXT    NOT NULL DEFAULT '',  -- JSON 文本：{"$.data.xxx": "yyy"}
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS executions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    status      TEXT    NOT NULL DEFAULT 'running',  -- running / done(全部通过) / failed(有失败)
    total       INTEGER NOT NULL DEFAULT 0,
    passed      INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    error       TEXT    NOT NULL DEFAULT '',         -- 批次级错误（v1 基本不用，留给异步执行器）
    duration_ms INTEGER NOT NULL DEFAULT 0,          -- 整批墙钟时间
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS execution_items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id      INTEGER NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
    case_id           INTEGER,                       -- 故意不设外键：用例删除后历史明细仍在
    case_name         TEXT    NOT NULL DEFAULT '',   -- 快照：执行那一刻的用例名
    passed            INTEGER NOT NULL DEFAULT 0,
    status_code       INTEGER,                       -- HTTP 状态码；请求都没发出去时为 NULL
    resp_code         INTEGER,                       -- 业务 code（非 JSON 响应时为 NULL）
    duration_ms       INTEGER NOT NULL DEFAULT 0,
    request_snapshot  TEXT    NOT NULL DEFAULT '',   -- JSON 文本：method/url/headers(脱敏)/params/body
    response_snapshot TEXT    NOT NULL DEFAULT '',   -- JSON 文本：status_code/body(截断)
    asserts           TEXT    NOT NULL DEFAULT '[]', -- JSON 文本：断言明细 [{item,expected,actual,passed,message}]
    created_at        TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS import_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER,                              -- 导入目标项目（可能导入失败则留空）
    source      TEXT    NOT NULL DEFAULT '',          -- 导入来源（文件名/说明）
    imported    INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    message     TEXT    NOT NULL DEFAULT '',          -- 跳过原因等补充信息
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_cases_project      ON cases(project_id);
CREATE INDEX IF NOT EXISTS idx_exec_project       ON executions(project_id);
CREATE INDEX IF NOT EXISTS idx_items_execution    ON execution_items(execution_id);
CREATE INDEX IF NOT EXISTS idx_items_case         ON execution_items(case_id);
