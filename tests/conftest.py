"""pytest fixtures：给"测平台的测试"提供隔离的 app / client。

分层思路（docs/07 详述）：
- 单测（test_assert_engine.py、test_runner.py 的非 integration 部分）：
  不起服务、不连真库，用 mock / 临时目录；
- 集成（标记 @pytest.mark.integration）：
  需要真实 demo-mall 跑在 5000（test_runner.py / test_web_api.py 里各有一条）；
- Web/API 测试（test_web_api.py）：Flask test client——不用起端口，
  直接在进程内调蓝图，session/cookie 行为与真实浏览器一致。
"""

import os
import sys

import pytest

# 保证从任何工作目录跑 pytest 都能 import 到项目根的 app/config 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.db import insert  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    """每个测试一个独立的临时 SQLite 库 + 独立实例化 app（互不污染）。"""
    test_config = {
        "DB_PATH": str(tmp_path / "test_platform.db"),
        "SECRET_KEY": "test-secret-key",
        "TESTING": True,
        "EXECUTION_CONCURRENCY": 3,   # 测试里用小并发，加快并暴露竞态
    }
    application = create_app(test_config)
    with application.app_context():
        insert("INSERT INTO users(username, password_hash) VALUES(?,?)",
               ("admin", hash_password("admin123")))
    yield application


@pytest.fixture()
def client(app):
    """未登录的 test client。"""
    return app.test_client()


@pytest.fixture()
def logged_in(client):
    """已登录（admin/admin123）的 test client，登录态存在 client 的 cookie jar 里。"""
    resp = client.post("/api/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200
    assert resp.get_json()["code"] == 0
    return client
