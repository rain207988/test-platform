"""平台 Web 页面与 REST API 的端到端测试（Flask test client，进程内不起端口）。

覆盖目标（对应验证要求第 4/6 条的自动化版）：
- 未登录：页面 302 到 /login，API 返回 401 + code 4010；
- 登录/登出/错密码业务码；
- 建项目 → 建用例 → 执行 → 结果查询全链路；
- 单条用例对"死端口"执行：错误降级、execution 状态=failed；
- 导入端点：重名自动加后缀；
- 页面关键元素（稳定 id）存在；
- integration 标记：对真实 demo-mall 执行一条用例并验证全绿。
"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import requests

DEAD_BASE = "http://127.0.0.1:9"      # 端口 9 基本必然连接拒绝：测"网络错误降级"
MALL_BASE = "http://127.0.0.1:5000"


def _case_payload(**overrides) -> dict:
    payload = {
        "name": "商品列表第一页",
        "method": "GET",
        "path": "/api/goods",
        "params": {"page": 1},
        "expect_status": 200,
        "expect_code": 0,
        "expect_fields": {"$.data.total": 10},
    }
    payload.update(overrides)
    return payload


def _create_project(client, name="测试项目", base_url=DEAD_BASE) -> int:
    resp = client.post("/api/projects", json={"name": name, "base_url": base_url})
    assert resp.get_json()["code"] == 0
    return resp.get_json()["data"]["id"]


# ---------------------------------------------------------------------------
# 登录与访问控制
# ---------------------------------------------------------------------------

class TestAuth:
    def test_health_no_auth(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.get_json()["code"] == 0

    def test_login_page_has_form(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'id="login-form"' in html
        assert 'id="username"' in html and 'id="password"' in html

    def test_page_redirects_when_not_logged_in(self, client):
        resp = client.get("/projects")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_api_returns_4010_when_not_logged_in(self, client):
        resp = client.get("/api/projects")
        assert resp.status_code == 401
        body = resp.get_json()
        assert body["code"] == 4010            # 与被测系统同款约定

    def test_login_wrong_password_code_1002(self, client):
        resp = client.post("/api/login", json={"username": "admin", "password": "nope"})
        body = resp.get_json()
        assert body["code"] == 1002

    def test_login_logout_flow(self, client):
        resp = client.post("/api/login", json={"username": "admin", "password": "admin123"})
        assert resp.get_json()["code"] == 0
        me = client.get("/api/me").get_json()
        assert me["data"]["username"] == "admin"
        client.post("/api/logout")
        assert client.get("/api/me").status_code == 401


# ---------------------------------------------------------------------------
# 项目 / 用例 CRUD
# ---------------------------------------------------------------------------

class TestProjectAndCaseCrud:
    def test_create_project(self, logged_in):
        pid = _create_project(logged_in, "商城项目", MALL_BASE)
        data = logged_in.get(f"/api/projects/{pid}").get_json()["data"]
        assert data["name"] == "商城项目"
        assert data["base_url"] == MALL_BASE

    def test_duplicate_project_name_conflict(self, logged_in):
        _create_project(logged_in, "重名项目")
        resp = logged_in.post("/api/projects", json={"name": "重名项目"})
        assert resp.get_json()["code"] == 1003

    def test_create_and_get_case(self, logged_in):
        pid = _create_project(logged_in)
        resp = logged_in.post(f"/api/projects/{pid}/cases", json=_case_payload())
        assert resp.get_json()["code"] == 0
        cid = resp.get_json()["data"]["id"]

        data = logged_in.get(f"/api/cases/{cid}").get_json()["data"]
        # 存库是 JSON 文本，对外必须解析回对象（前端/脚本不用再 loads）
        assert data["params"] == {"page": 1}
        assert data["expect_fields"] == {"$.data.total": 10}
        assert data["method"] == "GET"

    def test_case_validation_error(self, logged_in):
        pid = _create_project(logged_in)
        resp = logged_in.post(f"/api/projects/{pid}/cases",
                              json=_case_payload(name=" "))
        assert resp.get_json()["code"] == 1004
        resp = logged_in.post(f"/api/projects/{pid}/cases",
                              json=_case_payload(path="api/goods"))
        assert resp.get_json()["code"] == 1004

    def test_duplicate_case_name_gets_suffix(self, logged_in):
        """重名策略：不报错不覆盖，自动 -2 后缀。"""
        pid = _create_project(logged_in)
        logged_in.post(f"/api/projects/{pid}/cases", json=_case_payload(name="同名用例"))
        logged_in.post(f"/api/projects/{pid}/cases", json=_case_payload(name="同名用例"))
        cases = logged_in.get(f"/api/projects/{pid}/cases").get_json()["data"]
        names = sorted(c["name"] for c in cases)
        assert names == ["同名用例", "同名用例-2"]

    def test_update_and_delete_case(self, logged_in):
        pid = _create_project(logged_in)
        cid = logged_in.post(f"/api/projects/{pid}/cases",
                             json=_case_payload()).get_json()["data"]["id"]
        resp = logged_in.put(f"/api/cases/{cid}",
                             json=_case_payload(name="改名后的用例", expect_code=0))
        assert resp.get_json()["code"] == 0
        assert logged_in.get(f"/api/cases/{cid}").get_json()["data"]["name"] == "改名后的用例"
        assert logged_in.delete(f"/api/cases/{cid}").get_json()["code"] == 0
        assert logged_in.get(f"/api/cases/{cid}").status_code == 200  # HTTP 200 + 业务码
        assert logged_in.get(f"/api/cases/{cid}").get_json()["code"] == 2001


# ---------------------------------------------------------------------------
# 执行与结果查询（死端口：专测错误降级路径）
# ---------------------------------------------------------------------------

class TestExecution:
    def test_execute_single_case_dead_port(self, logged_in):
        pid = _create_project(logged_in, base_url=DEAD_BASE)
        cid = logged_in.post(f"/api/projects/{pid}/cases",
                             json=_case_payload()).get_json()["data"]["id"]
        resp = logged_in.post(f"/api/cases/{cid}/execute")
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["passed"] == 0 and data["failed"] == 1
        assert data["status"] == "failed"

        detail = logged_in.get(f"/api/executions/{data['execution_id']}").get_json()["data"]
        assert detail["total"] == 1 and detail["passed"] == 0
        item = detail["items"][0]
        assert item["asserts"][0]["item"] == "网络请求"
        assert item["status_code"] is None

    def test_batch_execute_and_stats(self, logged_in):
        pid = _create_project(logged_in, base_url=DEAD_BASE)
        for name in ("用例A", "用例B"):
            logged_in.post(f"/api/projects/{pid}/cases", json=_case_payload(name=name))
        resp = logged_in.post(f"/api/projects/{pid}/execute")
        data = resp.get_json()["data"]
        assert data["total"] == 2 and data["failed"] == 2

        stats = logged_in.get("/api/stats").get_json()["data"]
        assert stats["projects"] >= 1 and stats["cases"] >= 2
        assert stats["executions"] >= 1
        assert stats["last_execution"]["total"] == 2

        # 用例列表的徽标数据：两条用例都应带"最近结果=失败"
        cases = logged_in.get(f"/api/projects/{pid}/cases").get_json()["data"]
        assert all(c["last_passed"] == 0 for c in cases)

    def test_execution_detail_page_renders_details(self, logged_in):
        pid = _create_project(logged_in, base_url=DEAD_BASE)
        cid = logged_in.post(f"/api/projects/{pid}/cases",
                             json=_case_payload()).get_json()["data"]["id"]
        eid = logged_in.post(f"/api/cases/{cid}/execute").get_json()["data"]["execution_id"]
        resp = logged_in.get(f"/executions/{eid}")
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert "<details" in html                       # 折叠明细
        assert 'id="exec-pass-rate"' in html            # 顶部通过率
        assert 'id="item-asserts-' in html              # 断言明细表
        assert "网络请求" in html

    def test_executions_page_and_cases_page_elements(self, logged_in):
        pid = _create_project(logged_in)
        resp = logged_in.get(f"/projects/{pid}/cases")
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert 'id="cases-table"' in html
        assert 'id="btn-exec-all"' in html
        assert 'id="last-pass-rate"' in html
        assert 'id="badge-case-' in html or "还没有用例" in html

        assert logged_in.get("/projects").status_code == 200
        assert 'id="projects-table"' in logged_in.get("/projects").get_data(as_text=True)
        assert logged_in.get("/stats").status_code == 200


# ---------------------------------------------------------------------------
# 导入端点
# ---------------------------------------------------------------------------

class TestImport:
    def test_import_and_dedup(self, logged_in):
        payload = {
            "project": {"name": "导入测试项目", "base_url": MALL_BASE},
            "cases": [
                {"name": "健康检查", "method": "GET", "path": "/api/health",
                 "expect_code": 0},
                {"name": "健康检查", "method": "GET", "path": "/api/health",
                 "expect_code": 0},
                {"name": "坏用例", "method": "PUT", "path": "no-slash"},   # 非法路径
            ],
        }
        resp = logged_in.post("/api/import", json=payload)
        data = resp.get_json()["data"]
        assert data["imported"] == 2 and data["skipped"] == 1

        cases = logged_in.get(f"/api/projects/{data['project_id']}/cases").get_json()["data"]
        names = sorted(c["name"] for c in cases)
        assert names == ["健康检查", "健康检查-2"]       # 重名后缀 + 跳过非法

    def test_import_twice_same_project(self, logged_in):
        """重复导入落到同一项目（按项目名 get-or-create）。"""
        payload = {
            "project": {"name": "幂等导入项目", "base_url": MALL_BASE},
            "cases": [{"name": "用例1", "method": "GET", "path": "/api/health"}],
        }
        first = logged_in.post("/api/import", json=payload).get_json()["data"]
        second = logged_in.post("/api/import", json=payload).get_json()["data"]
        assert first["project_id"] == second["project_id"]
        cases = logged_in.get(f"/api/projects/{first['project_id']}/cases").get_json()["data"]
        assert {c["name"] for c in cases} == {"用例1", "用例1-2"}


# ---------------------------------------------------------------------------
# 集成：真实 demo-mall 全绿路径
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestAgainstRealMall:
    def test_execute_case_green(self, logged_in):
        if not self._mall_up():
            pytest.skip("demo-mall(5000) 未运行")
        pid = _create_project(logged_in, "真实商城项目", MALL_BASE)
        cid = logged_in.post(f"/api/projects/{pid}/cases",
                             json=_case_payload()).get_json()["data"]["id"]
        data = logged_in.post(f"/api/cases/{cid}/execute").get_json()["data"]
        assert data["status"] == "done"
        assert data["passed"] == 1 and data["failed"] == 0

    @staticmethod
    def _mall_up() -> bool:
        try:
            s = requests.Session()
            s.trust_env = False
            return s.get(f"{MALL_BASE}/api/health", timeout=2).status_code == 200
        except requests.RequestException:
            return False
