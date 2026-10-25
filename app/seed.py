"""初始化 / 重置平台数据库到"种子状态"。

用法（两种都支持，脚本里做了 sys.path 兜底）：
    python -m app.seed --reset     # 推荐：包内模块方式，import 一定正确
    python app/seed.py --reset     # 直接执行也行

--reset 会清空全部六张表并把自增 id 归零（RESET sqlite_sequence），
保证"重置后跑出来的 id、报告截图"都是确定性的——与 demo-mall/seed.py
同一设计哲学：确定性数据是可重复测试的前提。

种子内容：
- 管理员 admin/admin123（pbkdf2 摘要入库）
- 演示项目 "Demo商城接口"（base_url 指向被测系统 5000 端口）
- 3 条示例用例：健康检查 / 登录 / 商品列表——开箱即有东西可点、可执行
"""

import argparse
import json
import sys
import os

# 直接以脚本方式运行时（python app/seed.py）项目根目录不在 sys.path，
# 手动补上，保证 from app import ... 在两种运行方式下都能工作
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.db import execute, insert, query_one  # noqa: E402

SEED_ADMIN = ("admin", "admin123")

SEED_PROJECT = {
    "name": "Demo商城接口",
    "description": "被测系统 demo-mall 的接口用例集（可直接用 scripts/import_demo_mall.py 补充全量用例）",
    "base_url": "http://127.0.0.1:5000",
}

# 示例用例：全部是"无状态 + 必然绿"的用例，供开箱演示
SEED_CASES = [
    {
        "name": "[示例] 健康检查",
        "method": "GET",
        "path": "/api/health",
        "expect_status": 200,
        "expect_code": 0,
    },
    {
        "name": "[示例] 账号密码登录成功",
        "method": "POST",
        "path": "/api/auth/login",
        "body": {"username": "lemon", "password": "123456"},
        "expect_status": 200,
        "expect_code": 0,
        "expect_fields": {"$.data.user.username": "lemon", "$.data.expires_in": 1800},
    },
    {
        "name": "[示例] 商品列表默认第一页",
        "method": "GET",
        "path": "/api/goods",
        "expect_status": 200,
        "expect_code": 0,
        "expect_fields": {"$.data.total": 10, "$.data.page": 1},
    },
]


def reset_database():
    """清空六张表并重置自增 id（要求库已建表）。"""
    tables = ["execution_items", "executions", "cases", "import_logs",
              "projects", "users"]
    for table in tables:
        execute(f"DELETE FROM {table}")  # noqa: S608 表名来自白名单常量
    execute("DELETE FROM sqlite_sequence")  # 自增 id 归零，保证确定性
    seed_data()


def seed_data():
    """灌入管理员 + 演示项目 + 示例用例。"""
    admin_id = insert(
        "INSERT INTO users(username, password_hash) VALUES(?,?)",
        (SEED_ADMIN[0], hash_password(SEED_ADMIN[1])))
    project_id = insert(
        "INSERT INTO projects(name, description, base_url) VALUES(?,?,?)",
        (SEED_PROJECT["name"], SEED_PROJECT["description"], SEED_PROJECT["base_url"]))
    case_ids = []
    for case in SEED_CASES:
        case_ids.append(insert(
            "INSERT INTO cases(project_id, name, method, path, headers, params,"
            " body, auth_user, auth_pass, expect_status, expect_code, expect_fields)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, case["name"], case["method"], case["path"],
             json.dumps(case["headers"], ensure_ascii=False) if case.get("headers") else "",
             json.dumps(case["params"], ensure_ascii=False) if case.get("params") else "",
             json.dumps(case["body"], ensure_ascii=False) if case.get("body") else "",
             case.get("auth_user", ""), case.get("auth_pass", ""),
             case.get("expect_status"),
             case.get("expect_code"),
             json.dumps(case["expect_fields"], ensure_ascii=False)
             if case.get("expect_fields") else "")))
    print(f"[test-platform] 管理员 {SEED_ADMIN[0]}/{SEED_ADMIN[1]} (id={admin_id})")
    print(f"[test-platform] 演示项目 {SEED_PROJECT['name']!r} (id={project_id})，"
          f"示例用例 {len(case_ids)} 条")


def main():
    parser = argparse.ArgumentParser(description="test-platform 种子数据")
    parser.add_argument("--reset", action="store_true",
                        help="清空全部数据并重新灌种子（重置演示环境用）")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        if args.reset:
            if not query_one("SELECT COUNT(*) AS c FROM users"):
                print("[test-platform] 库为空，直接初始化种子数据")
                seed_data()
            else:
                reset_database()
                print("[test-platform] 已重置：六张表清空 + 种子数据重灌")
        else:
            if query_one("SELECT COUNT(*) AS c FROM users"):
                print("[test-platform] 已有数据，跳过（需要重置请加 --reset）")
            else:
                seed_data()
    print(f"[test-platform] 数据库：{app.config['DB_PATH']}")


if __name__ == "__main__":
    main()
