"""三层断言引擎（平台的"裁判"，与 api-test-framework/common/assertions.py 同源同思想）。

三层为什么缺一不可（以被测系统 demo-mall 为例）：
①HTTP 状态码：token 无效时服务端返回 HTTP 401——只看业务 code 会漏掉鉴权层问题；
②业务 code：业务失败时返回 HTTP 200 + code=1002——只看 HTTP 状态码会把
  "密码错误"也当成通过，这是国内系统最常见的响应约定；
③关键字段：code=0 不代表数据对，还得用 JSONPath 抽查具体返回值
 （如 $.data.user.username 必须是 lemon）。

与 pytest 版断言的关键差异（面试必讲）：
    pytest 版：assert_response() 一层不满足就 raise AssertionError——
              "fail fast"，服务 pytest 的"用例失败要终止"语义；
    平台版  ：check() 不抛异常，返回结构化明细列表——
              平台的报告要展示"这条用例共 5 条断言，4 过 1 挂，挂的是哪个字段、
              期望多少实际多少"。如果第一处失败就抛异常，后面的断言就没机会执行，
              报告只能显示一条报错堆栈，用户还得重跑才能看全貌。
    一句话：pytest 的断言面向"人肉看日志"，平台的断言面向"机器渲染报告"。

check() 返回结构（与 execution_items.asserts 列的 JSON 文本完全同构）：
    [
        {"item": "HTTP状态码",        "expected": 200,    "actual": 200,  "passed": true,  "message": "通过"},
        {"item": "业务code",          "expected": 0,      "actual": 1002, "passed": false, "message": "期望 0，实际 1002"},
        {"item": "字段 $.data.token", "expected": "...",  "actual": null, "passed": false, "message": "JSONPath 未命中（字段不存在）"},
    ]
"""

import json

from jsonpath import jsonpath

# 断言项的可读名字（报告里显示用）
ITEM_STATUS = "HTTP状态码"
ITEM_CODE = "业务code"
ITEM_FIELDS_PREFIX = "字段 "


def parse_body(resp):
    """尽量把响应体解析成 Python 对象；不是合法 JSON 返回 None。

    为什么吞掉解析异常：非 JSON 响应（如网关返回的 HTML 错误页）是测试里
    常见的真实输入，属于"被测系统的行为"，应当被断言捕获并展示，
    而不是让执行引擎当场崩掉。
    """
    try:
        return resp.json()
    except ValueError:  # json.JSONDecodeError 及 requests 的 JSONDecodeError 都是 ValueError 子类
        return None


def extract_jsonpath(data, expr: str):
    """按 JSONPath 取值；命中返回第一个值，未命中返回 None。

    注意 jsonpath 库"没有命中"时返回 False（不是 None），所以必须用真值判断
    而不是 `if matches is None`。局限：字段值本身是 JSON null 时也会被当成
    "未命中"——教学平台接受这个局限（与参考实现保持一致），文档里如实说明。
    """
    matches = jsonpath(data, expr)
    return matches[0] if matches else None


def _make_item(name: str, expected, actual, passed: bool, message: str = "") -> dict:
    """组装一条断言明细。message 保留人话原因，报告直接渲染不用再翻译。"""
    return {
        "item": name,
        "expected": expected,
        "actual": actual,
        "passed": passed,
        "message": message or ("通过" if passed else f"期望 {expected!r}，实际 {actual!r}"),
    }


def check(resp, expect: dict | None = None) -> list[dict]:
    """对一次 HTTP 响应做三层断言，返回断言明细列表（绝不抛异常）。

    expect 结构（与平台 cases 表的三列一一对应）：
        status_code: 200        # 第①层，可选（cases.expect_status）
        code: 0                 # 第②层，可选（cases.expect_code）
        fields:                 # 第③层，可选（cases.expect_fields，JSONPath -> 期望值）
            "$.data.user.username": "lemon"

    三层互相独立、全部执行：哪怕第①层挂了，②③层的明细照样给出——
    这正是"返回明细"相对"抛异常"的核心收益。
    """
    expect = expect or {}
    results: list[dict] = []
    body = parse_body(resp)

    # ---- 第①层：HTTP 状态码 ----
    if expect.get("status_code") is not None:
        actual = resp.status_code
        expected = expect["status_code"]
        results.append(_make_item(
            ITEM_STATUS, expected, actual, actual == expected))

    # ---- 第②层：业务错误码 ----
    if expect.get("code") is not None:
        expected = expect["code"]
        if body is None:
            results.append(_make_item(
                ITEM_CODE, expected, None, False, "响应体不是合法JSON"))
        else:
            actual = body.get("code")
            results.append(_make_item(
                ITEM_CODE, expected, actual, actual == expected))

    # ---- 第③层：关键字段（JSONPath） ----
    for expr, expected in (expect.get("fields") or {}).items():
        name = f"{ITEM_FIELDS_PREFIX}{expr}"
        if body is None:
            results.append(_make_item(
                name, expected, None, False, "响应体不是合法JSON"))
            continue
        actual = extract_jsonpath(body, expr)
        if actual is None:
            results.append(_make_item(
                name, expected, None, False, "JSONPath 未命中（字段不存在）"))
        else:
            # 注意不做 isinstance 严格类型校验：Python 里 199.0 == 199 为 True，
            # "后端把 int 改成 float"这类无害变更不应导致用例失败
            results.append(_make_item(
                name, expected, actual, actual == expected))

    return results


def all_passed(results: list[dict]) -> bool:
    """整条用例是否通过：明细为空视为通过（没配期望就不拦），有任一失败即失败。"""
    return all(r["passed"] for r in results)
