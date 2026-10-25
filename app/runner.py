"""执行引擎（平台的心脏）：把一条用例定义变成一次真实的 HTTP 请求 + 结构化断言结果。

职责边界（面试必讲）：
- execute_case：纯函数式——入参是"用例定义 dict + base_url"，出参是"结果 dict"，
  不碰数据库、不碰 Flask 上下文。这是它能被扔进线程池的前提：
  sqlite3 连接不能跨线程，Flask 的 g 也不能跨线程，所以"并发只发生在 HTTP 层，
  落库全部回到调用线程串行做"——用最小的复杂度换线程安全。
- execute_batch：调度 + 落库——先建 ThreadPoolExecutor 并发跑 execute_case，
  等全部完成后由调用线程把结果写进 execution_items / executions。
- token 自动化：用例配了 auth_user 就先调被测系统的登录接口拿 token，
  进程内缓存（按 base_url+账号 维度），避免每个用例都登录一次。

为什么平台用例默认要求"无状态"（用例间不共享数据、不依赖执行顺序）：
- 并发执行时顺序天然不可控，有状态的用例（A 先加购 B 再查购物车）在并发下
  会随机互相踩；
- 无状态用例才能"任意子集重跑、任意并发跑"，这是平台化执行的前提；
- 需要链路的场景（登录→加购→下单）属于"多步编排"，是 v2 的方向
  （参考 api-test-framework/data/chain_data.yaml 的 steps+extract 设计）。
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from app import assert_engine
from app.db import execute as db_execute
from app.db import get_db
from app.db import insert as db_insert

import config

# ---------------------------------------------------------------------------
# 进程内 token 缓存：{(base_url, username, password): (token, 过期时间戳)}
# 为什么放进程内而不是数据库：token 本来就是短时效凭证，重启即失效；
# 平台是单进程部署（Flask dev / 一个 worker），进程内 dict 就是最简方案。
# 真要水平扩容才需要换 Redis——v1 不过度设计。
# ---------------------------------------------------------------------------
_token_cache: dict[tuple, tuple[str, float]] = {}
_token_lock = threading.Lock()

# 每线程一个 requests.Session：Session 复用 TCP 连接明显提速，
# 但 requests.Session 官方不保证线程安全，所以用 threading.local 做到"线程各自持有"
_thread_local = threading.local()


def _get_session() -> requests.Session:
    """取当前线程专用的 Session。

    trust_env=False 是 Windows 上常见的坑：系统代理(如 127.0.0.1:7897)开着时，
    requests 默认会读环境变量/注册表里的代理设置，把发往 127.0.0.1:5000 的
    请求也丢给代理，轻则慢重则直接连不上。平台只打内网被测系统，干脆禁用。
    """
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.trust_env = False
        _thread_local.session = session
    return session


def _send(method: str, url: str, *, params=None, json_body=None,
          headers=None, timeout=None) -> requests.Response:
    """所有出站 HTTP 的唯一出口（方便测试 monkeypatch，也方便统一加埋点）。"""
    return _get_session().request(
        method, url, params=params, json=json_body, headers=headers,
        timeout=timeout or config.REQUEST_TIMEOUT)


def fetch_token(base_url: str, username: str, password: str) -> str:
    """用账号密码向被测系统登录换 token，带进程内缓存。

    加锁的原因：并发批量执行时 50 条用例可能同时发现"没有缓存"，
    不加锁就并发发 50 次登录请求——功能上没错，但既浪费又可能触发被测系统
    的登录限流。锁内"查缓存→没有→登录→写缓存"保证了同一账号只登录一次。
    """
    key = (base_url.rstrip("/"), username, password)
    with _token_lock:
        cached = _token_cache.get(key)
        if cached and cached[1] > time.time():
            return cached[0]

        resp = _send("POST", key[0] + "/api/auth/login",
                     json_body={"username": username, "password": password})
        body = assert_engine.parse_body(resp)
        token = (body or {}).get("data", {}).get("token") if isinstance(body, dict) else None
        if not token:
            # 登录失败不抛异常：返回空串，用例执行时会因为没有 Bearer 头
            # 收到 401，断言明细自然显示失败原因——错误信息顺着既有链路走
            return ""
        _token_cache[key] = (token, time.time() + config.TOKEN_TTL_SECONDS)
        return token


def clear_token_cache():
    """清空 token 缓存（测试用/被测系统重置数据后可手动调）。"""
    with _token_lock:
        _token_cache.clear()


def _mask_headers(headers: dict) -> dict:
    """快照里的请求头脱敏：Bearer token 只留末 6 位便于对账，其余打码。

    报告会被截图/分享，完整 token 泄进报告是真实企业里出过的事故。
    """
    masked = {}
    for k, v in (headers or {}).items():
        if k.lower() == "authorization" and isinstance(v, str) and len(v) > 12:
            masked[k] = f"{v[:7]}****{v[-6:]}"
        else:
            masked[k] = v
    return masked


def _load_json_field(raw: str):
    """把库里存的 JSON 文本反序列化；空串返回 None（= 请求里不带这一段）。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    return json.loads(raw)


def build_expect_from_case(case: dict) -> dict:
    """把 cases 表的三列期望拼成 assert_engine.check 需要的 expect dict。

    关键细节：expect_code=0 是合法期望，判空必须用 is not None，
    写成 `if expect_code:` 会把 0 当成"没配"——三层断言直接少一层，
    这种 bug 报告上完全看不出来，属于"越隐蔽越致命"。
    """
    expect: dict = {}
    if case.get("expect_status") is not None:
        expect["status_code"] = case["expect_status"]
    if case.get("expect_code") is not None:
        expect["code"] = case["expect_code"]
    fields = _load_json_field(case.get("expect_fields"))
    if fields:
        expect["fields"] = fields
    return expect


def execute_case(case: dict, base_url: str, timeout: int = None) -> dict:
    """执行单条用例，返回结果 dict（永不抛异常——单条用例的任何错误都降级为"失败明细"）。

    为什么"永不抛异常"是硬要求：execute_batch 里 50 条用例并发跑，
    其中 1 条构造请求时 KeyError/TypeError，如果异常冒出来，
    整批 future 收集都会被打断，其他 49 条的结果也跟着丢。
    把"任何错误都变成一条 failed 断言明细"后，批次永远能完整收口。

    参数 case 是 cases 表一行（dict 形式），base_url 来自所属项目。
    """
    case_id = case.get("id")
    name = case.get("name") or f"case#{case_id}"
    base_url = (base_url or "").rstrip("/")

    result = {
        "case_id": case_id,
        "case_name": name,
        "passed": False,
        "status_code": None,
        "resp_code": None,
        "duration_ms": 0,
        "request_snapshot": "",
        "response_snapshot": "",
        "asserts": [],
        "error": "",
    }

    try:
        # ---- 1. 从用例定义还原请求要素（库里存 JSON 文本，读出要 loads） ----
        method = (case.get("method") or "GET").upper()
        path = case.get("path") or "/"
        headers = _load_json_field(case.get("headers")) or {}
        params = _load_json_field(case.get("params"))
        body = _load_json_field(case.get("body"))

        # ---- 2. 鉴权自动化：配了 auth_user 就先登录换 token ----
        # setdefault：用例自己显式写的 Authorization 优先级更高
        #（例如"伪造 token"用例必须发假 token，不能被自动登录顶掉）
        if case.get("auth_user"):
            token = fetch_token(base_url, case["auth_user"], case.get("auth_pass") or "")
            headers.setdefault("Authorization", f"Bearer {token}")

        url = base_url + path

        # ---- 3. 计时发请求 ----
        start = time.perf_counter()
        resp = _send(method, url, params=params, json_body=body,
                     headers=headers, timeout=timeout)
        result["duration_ms"] = round((time.perf_counter() - start) * 1000)

        # ---- 4. 三层断言（结构化明细，不抛异常） ----
        expect = build_expect_from_case(case)
        asserts = assert_engine.check(resp, expect)
        result["asserts"] = asserts
        result["passed"] = assert_engine.all_passed(asserts)
        result["status_code"] = resp.status_code
        parsed = assert_engine.parse_body(resp)
        if isinstance(parsed, dict):
            result["resp_code"] = parsed.get("code")

        # ---- 5. 快照（请求脱敏 + 响应截断），报告页直接渲染 ----
        result["request_snapshot"] = json.dumps({
            "method": method, "url": url,
            "headers": _mask_headers(headers),
            "params": params, "body": body,
        }, ensure_ascii=False)
        result["response_snapshot"] = json.dumps({
            "status_code": resp.status_code,
            "body": resp.text[:config.SNAPSHOT_BODY_LIMIT],
        }, ensure_ascii=False)
    except requests.RequestException as exc:
        # 网络层失败（连不上/超时）：转成一条失败断言，批次不受影响。
        # 归类为"网络请求"项而不是硬塞进三层之一——它压根没到断言那一步
        result["asserts"] = [{
            "item": "网络请求", "expected": "响应可达",
            "actual": str(exc)[:300], "passed": False,
            "message": f"请求未到达被测系统：{exc.__class__.__name__}",
        }]
        result["error"] = f"{exc.__class__.__name__}: {exc}"
        result["request_snapshot"] = json.dumps({
            "method": method, "url": base_url + path,
            "headers": _mask_headers(headers),
            "params": params, "body": body,
        }, ensure_ascii=False)
        result["response_snapshot"] = json.dumps({
            "status_code": None, "body": result["error"],
        }, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 兜底：用例定义本身有脏数据也不能拖垮批次
        result["asserts"] = [{
            "item": "用例执行", "expected": "无异常",
            "actual": str(exc)[:300], "passed": False,
            "message": f"用例执行出错：{exc.__class__.__name__}",
        }]
        result["error"] = f"{exc.__class__.__name__}: {exc}"
    return result


def execute_batch(execution_id: int, case_rows: list[dict], base_url: str,
                  concurrency: int = None) -> dict:
    """并发执行一批用例并把结果落库，返回批次摘要。

    并发模型（重要）：HTTP 请求在 worker 线程里并发发（瓶颈在网络 IO，
    线程池最合适）；sqlite 落库在调用线程里串行做（sqlite 连接不能跨线程）。
    即"并发 IO、串行写库"，用最简单的模型规避两类线程安全问题。

    execution_id 由调用方（API 层）先建好 executions 行（status=running）传入，
    本函数只负责跑 + 更新——"建记录"和"跑批"分开，将来异步执行器
    （Celery/RQ）可以直接复用这个函数。
    """
    concurrency = concurrency or config.EXECUTION_CONCURRENCY
    start = time.perf_counter()

    # results 保持与 case_rows 相同顺序：报告按用例 id 排序展示更稳定
    results: list[dict | None] = [None] * len(case_rows)
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        future_to_idx = {
            pool.submit(execute_case, case, base_url): i
            for i, case in enumerate(case_rows)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            # execute_case 承诺不抛异常，这里仍然 try 一次兜底：
            # 平台自身的 bug 不应该让"跑批"这个动作半途而废
            try:
                results[idx] = future.result()
            except Exception as exc:  # noqa: BLE001
                case = case_rows[idx]
                results[idx] = {
                    "case_id": case.get("id"), "case_name": case.get("name", ""),
                    "passed": False, "status_code": None, "resp_code": None,
                    "duration_ms": 0, "request_snapshot": "",
                    "response_snapshot": "",
                    "asserts": [{"item": "调度", "expected": "任务完成",
                                 "actual": str(exc)[:200], "passed": False,
                                 "message": "执行任务异常中断"}],
                    "error": str(exc),
                }

    # ---- 落库：全部跑完才写（v1 简化；异步化演进见 docs/05） ----
    passed = 0
    for r in results:
        r = r or {}
        is_pass = 1 if r.get("passed") else 0
        passed += is_pass
        db_insert(
            "INSERT INTO execution_items(execution_id, case_id, case_name, passed,"
            " status_code, resp_code, duration_ms, request_snapshot,"
            " response_snapshot, asserts) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (execution_id, r.get("case_id"), r.get("case_name", ""), is_pass,
             r.get("status_code"), r.get("resp_code"), r.get("duration_ms", 0),
             r.get("request_snapshot", ""), r.get("response_snapshot", ""),
             json.dumps(r.get("asserts") or [], ensure_ascii=False)),
        )

    failed = len(results) - passed
    duration_ms = round((time.perf_counter() - start) * 1000)
    # status 口径：全部通过=done，有任一失败=failed（error 列留给批次级异常）
    status = "done" if failed == 0 else "failed"
    db_execute(
        "UPDATE executions SET status=?, total=?, passed=?, failed=?, duration_ms=?"
        " WHERE id=?",
        (status, len(results), passed, failed, duration_ms, execution_id),
    )
    get_db().commit()

    return {
        "status": status, "total": len(results), "passed": passed,
        "failed": failed, "duration_ms": duration_ms,
    }
