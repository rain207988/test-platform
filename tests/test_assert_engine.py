"""断言引擎纯单测：不起服务、不连库、不打真 HTTP——全部用内存里构造的 Response。

覆盖目标（docs/07 的"断言引擎测试矩阵"）：
- 三层断言全过 / 部分过；
- JSONPath 未命中（字段不存在）；
- 响应体不是合法 JSON（网关 HTML 错误页）；
- 期望为空时返回空明细；
- 数值 199 == 199.0（int/float 宽松相等）；
- 列表下标 JSONPath（$.data.list[0].id）。
"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from app import assert_engine


def make_resp(status: int = 200, body: dict | None = None, raw: str | None = None):
    """构造内存 Response（requests.Response 可以直接塞 _content，无需起服务）。"""
    resp = requests.Response()
    resp.status_code = status
    if raw is not None:
        resp._content = raw.encode("utf-8")
    elif body is not None:
        resp._content = json.dumps(body, ensure_ascii=False).encode("utf-8")
    else:
        resp._content = b""
    resp.headers["Content-Type"] = "application/json"
    return resp


def test_all_three_layers_pass():
    resp = make_resp(200, {"code": 0, "message": "ok",
                           "data": {"user": {"username": "lemon", "nickname": "柠檬"},
                                    "token": "abc", "expires_in": 1800}})
    expect = {
        "status_code": 200,
        "code": 0,
        "fields": {"$.data.user.username": "lemon", "$.data.expires_in": 1800},
    }
    results = assert_engine.check(resp, expect)
    assert len(results) == 4                       # 1 状态码 + 1 code + 2 字段
    assert all(r["passed"] for r in results)
    assert assert_engine.all_passed(results)
    # 明细结构必须四键齐全（落库 JSON 与报告渲染都依赖这个结构）
    assert set(results[0].keys()) == {"item", "expected", "actual", "passed", "message"}


def test_partial_fail_all_items_reported():
    """部分失败：失败的断言不能让后面的断言"没机会跑"——这是平台版与 pytest 版的核心差异。"""
    resp = make_resp(200, {"code": 1002, "message": "密码错误", "data": None})
    expect = {
        "status_code": 200,
        "code": 0,                                   # 会失败
        "fields": {"$.message": "密码错误",           # 会通过
                   "$.data.token": "abc"},           # 会失败（data 是 None）
    }
    results = assert_engine.check(resp, expect)
    assert len(results) == 4
    passed_flags = [r["passed"] for r in results]
    assert passed_flags == [True, False, True, False]   # 全部执行，各自独立判定
    assert not assert_engine.all_passed(results)


def test_status_code_fail():
    resp = make_resp(401, {"code": 4010, "message": "未登录或 token 无效/过期"})
    results = assert_engine.check(resp, {"status_code": 200, "code": 4010})
    assert results[0]["passed"] is False
    assert results[0]["item"] == "HTTP状态码"
    assert results[0]["expected"] == 200
    assert results[0]["actual"] == 401
    assert results[1]["passed"] is True


def test_jsonpath_miss():
    resp = make_resp(200, {"code": 0, "data": {"total": 10}})
    results = assert_engine.check(resp, {"fields": {"$.data.not_exist": 1}})
    assert results[0]["passed"] is False
    assert results[0]["actual"] is None
    assert "JSONPath 未命中" in results[0]["message"]


def test_list_index_jsonpath():
    resp = make_resp(200, {"code": 0, "data": {"list": [{"id": 4}, {"id": 5}]}})
    results = assert_engine.check(resp, {"fields": {"$.data.list[0].id": 4}})
    assert results[0]["passed"] is True


def test_non_json_response():
    """网关返回 HTML 错误页：code/字段断言都应失败且说明原因，而不是抛异常。"""
    resp = make_resp(502, raw="<html>Bad Gateway</html>")
    results = assert_engine.check(resp, {"status_code": 200, "code": 0,
                                         "fields": {"$.data.token": "x"}})
    assert len(results) == 3
    assert results[1]["passed"] is False
    assert "不是合法JSON" in results[1]["message"]
    assert results[2]["passed"] is False
    assert "不是合法JSON" in results[2]["message"]


def test_empty_expect_returns_empty_list():
    """没配任何期望：不拦（只发请求记录快照的场景），all_passed 视为通过。"""
    resp = make_resp(200, {"code": 0})
    results = assert_engine.check(resp, None)
    assert results == []
    assert assert_engine.all_passed(results)


def test_int_float_equal():
    """后端把 199 改成 199.0 不应导致用例失败（Python 数值相等语义）。"""
    resp = make_resp(200, {"code": 0, "data": {"price": 199.0, "total_amount": 1299}})
    results = assert_engine.check(resp, {"fields": {"$.data.price": 199,
                                                    "$.data.total_amount": 1299.0}})
    assert all(r["passed"] for r in results)


def test_chinese_field_value():
    """中文期望值：整个链路（YAML -> JSON 存储 -> 断言）不能乱码。"""
    resp = make_resp(200, {"code": 0, "message": "密码错误"})
    results = assert_engine.check(resp, {"fields": {"$.message": "密码错误"}})
    assert results[0]["passed"] is True
