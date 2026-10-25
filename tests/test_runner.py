"""执行引擎测试：分两层。

- 非集成部分：monkeypatch runner._send（唯一 HTTP 出口）模拟被测系统，
  验证请求构造 / token 自动登录与缓存 / 网络错误降级——毫秒级，不依赖任何服务；
- integration 标记部分：对真实 demo-mall(5000) 执行单条与并发批量，
  验证结果结构与通过数（需要 `python run.py --port 5000` 先起被测系统，
  且跑前建议 curl -X POST .../api/test/reset 重置种子数据）。
"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import requests

from app import runner
from app.db import insert, query_one


def make_resp(status: int, body: dict):
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps(body, ensure_ascii=False).encode("utf-8")
    resp.headers["Content-Type"] = "application/json"
    return resp


@pytest.fixture(autouse=True)
def _clean_token_cache():
    """token 缓存是模块级全局，测试间必须清掉，否则用例互相影响。"""
    runner.clear_token_cache()
    yield
    runner.clear_token_cache()


def _case(**overrides) -> dict:
    """一条标准平台用例定义（与 cases 表列同构）。"""
    case = {
        "id": 1, "name": "示例用例", "method": "GET", "path": "/api/goods",
        "headers": "", "params": json.dumps({"page": 1}), "body": "",
        "auth_user": "", "auth_pass": "",
        "expect_status": 200, "expect_code": 0,
        "expect_fields": json.dumps({"$.data.total": 10}),
    }
    case.update(overrides)
    return case


# ===========================================================================
# 单测部分（mock HTTP）
# ===========================================================================

class TestExecuteCase:
    def test_builds_request_and_passes(self, monkeypatch):
        """JSON 文本字段反序列化 + URL 拼接 + 三层断言全过。"""
        captured = {}

        def fake_send(method, url, **kwargs):
            captured.update(method=method, url=url, params=kwargs.get("params"))
            return make_resp(200, {"code": 0, "data": {"total": 10, "page": 1}})

        monkeypatch.setattr(runner, "_send", fake_send)
        result = runner.execute_case(_case(), "http://fake-mall")

        assert captured["method"] == "GET"
        assert captured["url"] == "http://fake-mall/api/goods"
        assert captured["params"] == {"page": 1}
        assert result["passed"] is True
        assert result["status_code"] == 200
        assert result["resp_code"] == 0
        assert len(result["asserts"]) == 3          # 状态码 + code + 字段
        assert result["error"] == ""
        # 快照可被 json.loads（落库前最后一道保障）
        snap = json.loads(result["request_snapshot"])
        assert snap["method"] == "GET" and snap["params"] == {"page": 1}

    def test_auto_login_and_bearer(self, monkeypatch):
        """配了 auth_user：先登录换 token，再带 Bearer 头发正式请求。"""
        calls = []

        def fake_send(method, url, **kwargs):
            calls.append({"method": method, "url": url, "json": kwargs.get("json_body"),
                          "headers": kwargs.get("headers")})
            if url.endswith("/api/auth/login"):
                assert kwargs["json_body"] == {"username": "lemon", "password": "123456"}
                return make_resp(200, {"code": 0, "data": {"token": "tok-123"}})
            return make_resp(200, {"code": 0, "data": {"username": "lemon"}})

        monkeypatch.setattr(runner, "_send", fake_send)
        case = _case(method="GET", path="/api/auth/me",
                     params="", expect_fields=json.dumps({"$.data.username": "lemon"}),
                     auth_user="lemon", auth_pass="123456")
        result = runner.execute_case(case, "http://fake-mall")

        assert len(calls) == 2                       # 1 次登录 + 1 次正式请求
        assert calls[0]["url"].endswith("/api/auth/login")
        auth_header = calls[1]["headers"]["Authorization"]
        assert auth_header == "Bearer tok-123"
        assert result["passed"] is True

    def test_token_cached_across_cases(self, monkeypatch):
        """同账号第二条用例不再登录（进程内缓存生效）。"""
        login_count = {"n": 0}

        def fake_send(method, url, **kwargs):
            if url.endswith("/api/auth/login"):
                login_count["n"] += 1
                return make_resp(200, {"code": 0, "data": {"token": "tok-abc"}})
            return make_resp(200, {"code": 0, "data": {}})

        monkeypatch.setattr(runner, "_send", fake_send)
        case = _case(path="/api/cart", params="", auth_user="lemon", auth_pass="123456")
        runner.execute_case(case, "http://fake-mall")
        runner.execute_case(case, "http://fake-mall")
        assert login_count["n"] == 1

    def test_explicit_authorization_wins(self, monkeypatch):
        """"伪造 token"类用例：headers 里显式写的 Authorization 优先于自动登录。
        （快照里的头是脱敏展示的：中间打码、只留末 6 位，用于确认"是伪造值而非真 token"）"""
        def fake_send(method, url, **kwargs):
            if url.endswith("/api/auth/login"):
                return make_resp(200, {"code": 0, "data": {"token": "real-token"}})
            return make_resp(401, {"code": 4010, "message": "未登录或 token 无效/过期"})

        monkeypatch.setattr(runner, "_send", fake_send)
        case = _case(path="/api/auth/me", params="",
                     headers=json.dumps({"Authorization": "Bearer forged.token.value"}),
                     auth_user="lemon", auth_pass="123456",
                     expect_status=401, expect_code=4010, expect_fields="")
        result = runner.execute_case(case, "http://fake-mall")
        snap = json.loads(result["request_snapshot"])
        # "forged.token.value" 末 6 位是 ".value"：说明请求发出去的确实是伪造头
        assert snap["headers"]["Authorization"] == "Bearer ****.value"
        assert "real-token" not in snap["headers"]["Authorization"]  # 真 token 没有顶掉伪造头
        # 401 断言按期望判定通过（这条用例本来就是测"要收到 401"）
        assert result["passed"] is True

    def test_token_masked_in_snapshot(self, monkeypatch):
        """快照里的 Bearer token 必须脱敏（报告防泄密）。"""
        def fake_send(method, url, **kwargs):
            if url.endswith("/api/auth/login"):
                return make_resp(200, {"code": 0, "data": {"token": "a" * 32 + "tail66"}})
            return make_resp(200, {"code": 0, "data": {}})

        monkeypatch.setattr(runner, "_send", fake_send)
        case = _case(path="/api/cart", params="", auth_user="lemon", auth_pass="123456")
        result = runner.execute_case(case, "http://fake-mall")
        snap = json.loads(result["request_snapshot"])
        assert "a" * 32 not in snap["headers"]["Authorization"]
        assert snap["headers"]["Authorization"].endswith("tail66")

    def test_network_error_degrades_to_failed_item(self):
        """连不上被测系统：不抛异常，转成"网络请求"失败明细，批次能继续。"""
        result = runner.execute_case(_case(), "http://127.0.0.1:9")  # 9 端口必拒绝
        assert result["passed"] is False
        assert result["asserts"][0]["item"] == "网络请求"
        assert result["status_code"] is None
        assert result["error"] != ""

    def test_dirty_expect_fields_json(self, monkeypatch):
        """脏数据兜底：expect_fields 不是合法 JSON → 用例级失败而不是引擎崩溃。
        （mock 一个正常响应，让流程走到"解析期望"那一步才会触发脏数据异常）"""
        monkeypatch.setattr(
            runner, "_send",
            lambda m, url, **kw: make_resp(200, {"code": 0, "data": {}}))
        result = runner.execute_case(_case(expect_fields="{bad json"), "http://fake-mall")
        assert result["passed"] is False
        assert result["asserts"][0]["item"] == "用例执行"

    def test_expect_code_zero_must_not_be_ignored(self, monkeypatch):
        """经典坑回归：expect_code=0 是合法期望，不能被 `if code:` 误判成没配。"""
        def fake_send(method, url, **kwargs):
            return make_resp(200, {"code": 1002, "message": "密码错误"})

        monkeypatch.setattr(runner, "_send", fake_send)
        result = runner.execute_case(_case(expect_fields=""), "http://fake-mall")
        items = [a["item"] for a in result["asserts"]]
        assert "业务code" in items                    # code=0 的断言确实执行了
        assert result["passed"] is False


# ===========================================================================
# 集成部分（真实 demo-mall，5000 端口）
# ===========================================================================

def _mall_up() -> bool:
    try:
        s = requests.Session()
        s.trust_env = False
        resp = s.get("http://127.0.0.1:5000/api/health", timeout=2)
        return resp.status_code == 200
    except requests.RequestException:
        return False


@pytest.mark.integration
class TestAgainstRealMall:
    def test_execute_single_case(self, app):
        if not _mall_up():
            pytest.skip("demo-mall(5000) 未运行")
        with app.app_context():
            result = runner.execute_case(
                _case(name="商品列表第一页"), "http://127.0.0.1:5000")
        assert result["passed"] is True
        assert result["status_code"] == 200
        assert result["resp_code"] == 0
        assert result["duration_ms"] > 0

    def test_execute_batch_concurrent(self, app):
        """并发批量：3 条用例（含 1 条需自动登录），全部通过并正确落库。"""
        if not _mall_up():
            pytest.skip("demo-mall(5000) 未运行")
        base_url = "http://127.0.0.1:5000"
        with app.app_context():
            project_id = insert(
                "INSERT INTO projects(name, base_url) VALUES(?,?)",
                ("集成测试项目", base_url))
            rows = [
                _case(id=None, name="健康检查", path="/api/health", params="",
                      expect_fields=""),
                _case(id=None, name="登录成功", method="POST", path="/api/auth/login",
                      params="", body=json.dumps({"username": "lemon", "password": "123456"}),
                      expect_fields=json.dumps({"$.data.user.username": "lemon"})),
                _case(id=None, name="查购物车需登录", path="/api/cart", params="",
                      auth_user="lemon", auth_pass="123456", expect_fields=""),
            ]
            for r in rows:
                r["id"] = insert(
                    "INSERT INTO cases(project_id, name, method, path, params, body,"
                    " auth_user, auth_pass, expect_status, expect_code, expect_fields)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (project_id, r["name"], r["method"], r["path"], r["params"],
                     r["body"], r["auth_user"], r["auth_pass"],
                     r["expect_status"], r["expect_code"], r["expect_fields"]))

            execution_id = insert(
                "INSERT INTO executions(project_id, status, total) VALUES(?,?,?)",
                (project_id, "running", len(rows)))
            summary = runner.execute_batch(execution_id, rows, base_url, concurrency=3)

            assert summary["total"] == 3
            assert summary["passed"] == 3
            assert summary["failed"] == 0
            assert summary["status"] == "done"
            assert summary["duration_ms"] > 0

            row = query_one("SELECT * FROM executions WHERE id=?", (execution_id,))
            assert row["status"] == "done" and row["passed"] == 3 and row["total"] == 3
