"""SQLite 连接与通用小工具（参照 demo-mall/app/db.py 的风格）。

为什么用 sqlite3 而不是 MySQL：
- 目标是"任何人 clone 后一条命令把平台跑起来"，零安装零配置；
- 教学平台的数据量（几百条用例/执行记录）远够用；
- SQL 写法与 MySQL 几乎一致，换库只动这一个文件。

与 demo-mall 版本的两处刻意差异（教学点）：
1. DB 路径从 current_app.config 取而不是 import 常量——
   这样 create_app({"DB_PATH": 临时路径}) 就能让测试用独立的临时库，
   平台自己被测试时不碰开发库；
2. execute/insert 自动 commit——平台的写操作全部走这两个助手，
   避免"每个调用点都要记得 commit"的经典坑（忘了 commit 表现是"接口说成功，
   重启数据没了"，极难排查）。
"""

import os
import sqlite3

from flask import current_app, g


def get_db() -> sqlite3.Connection:
    """Flask 的 g 对象：一个请求/应用上下文内复用同一个连接，请求结束自动关闭。

    row_factory 设为 sqlite3.Row，让我们能用 row["name"] 按列名取值，
    再也不用脆弱的 row[2] 下标。
    """
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DB_PATH"])
        g.db.row_factory = sqlite3.Row
        # SQLite 的外键约束默认是关的（为了兼容老库），每个连接都要显式打开，
        # 否则 schema 里的 ON DELETE CASCADE 全部失效
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query_one(sql: str, args: tuple = ()):
    """查一行，没有返回 None。"""
    return get_db().execute(sql, args).fetchone()


def query_all(sql: str, args: tuple = ()):
    """查多行，恒返回 list（空结果也是 []，调用方不用判 None）。"""
    return get_db().execute(sql, args).fetchall()


def execute(sql: str, args: tuple = ()) -> int:
    """执行写语句，返回受影响行数 rowcount，并自动提交。

    用途：UPDATE / DELETE。INSERT 请用 insert()（需要 lastrowid）。
    """
    cur = get_db().execute(sql, args)
    get_db().commit()
    return cur.rowcount


def insert(sql: str, args: tuple = ()) -> int:
    """执行 INSERT，返回自增主键 lastrowid，并自动提交。"""
    cur = get_db().execute(sql, args)
    get_db().commit()
    return cur.lastrowid


def init_db(db_path: str):
    """执行 schema.sql 建表（幂等：全部 CREATE TABLE IF NOT EXISTS）。"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
    conn = sqlite3.connect(db_path)
    try:
        with open(schema_path, encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()
    finally:
        conn.close()
