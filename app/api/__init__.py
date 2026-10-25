"""平台自己的 REST API（给前端 fetch 用，也给 scripts/import_demo_mall.py 用）。

响应格式刻意与被测系统 demo-mall 同构：{"code":0,"message":"ok","data":{...}}，
业务失败 HTTP 200 + code≠0，未登录 HTTP 401 + code 4010。
理由见 docs/03 与 docs/07：平台自己也是一套"被测系统"，
响应约定一致后，"用平台测平台"的测试代码可以复用同一套三层断言思想。

路由总览（全部 JSON，除 /api/login 外都要登录）：
    POST /api/login                登录（写 session）
    POST /api/logout               退出
    GET  /api/me                   当前用户
    GET  /api/stats                全局统计（项目数/用例数/批次数/最近通过率）
    POST /api/import               YAML 导入（importer.import_payload）
    CRUD /api/projects[/<id>]      项目增删改查
    POST /api/projects/<id>/execute          一键执行整个项目
    CRUD /api/projects/<id>/cases 与 /api/cases/<id>  用例增删改查
    POST /api/cases/<id>/execute             执行单条用例（记为一批 total=1）
    GET  /api/executions[?project_id=]       批次列表
    GET  /api/executions/<id>                批次详情（含全部 item 明细）
"""

import json

from flask import Blueprint, current_app, jsonify, request

from app import importer, runner
from app.auth import api_login_required, verify_password
from app.db import execute as db_execute
from app.db import insert as db_insert
from app.db import query_all, query_one

api_bp = Blueprint("api", __name__, url_prefix="/api")

# 平台 API 的业务码（刻意对齐 demo-mall 的码表，含义一致，降低心智负担）
CODE_OK = 0
CODE_NOT_FOUND = 2001
CODE_CONFLICT = 1003
CODE_BAD_PARAM = 1004
CODE_AUTH = 4010


def ok(data=None):
    return jsonify({"code": CODE_OK, "message": "ok", "data": data})


def fail(code: int, message: str, http_status: int = 200):
    return jsonify({"code": code, "message": message, "data": None}), http_status


def get_json_body() -> dict:
    """取 JSON 请求体；取不到返回 {}（silent=True 避免裸 415 干扰前端判断）。"""
    return request.get_json(silent=True) or {}


def parse_json_text(raw: str):
    """库里存的 JSON 文本 -> Python 对象；空串/坏数据返回 None/原样。"""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _get_case_or_fail(case_id: int):
    return query_one("SELECT * FROM cases WHERE id=?", (case_id,))


def _get_project_or_fail(project_id: int):
    return query_one("SELECT * FROM projects WHERE id=?", (project_id,))


def _case_out(row) -> dict:
    """cases 行 -> 对外 JSON（JSON 文本字段解析回对象，前端不用再 loads）。"""
    d = dict(row)
    for field in ("headers", "params", "body", "expect_fields"):
        d[field] = parse_json_text(d.get(field)) or {}
    return d


def _execution_out(row) -> dict:
    d = dict(row)
    d["pass_rate"] = round(d["passed"] * 100 / d["total"], 1) if d["total"] else None
    return d


def _item_out(row) -> dict:
    d = dict(row)
    d["request_snapshot"] = parse_json_text(d.get("request_snapshot")) or {}
    d["response_snapshot"] = parse_json_text(d.get("response_snapshot")) or {}
    d["asserts"] = parse_json_text(d.get("asserts")) or []
    return d


# ---------------------------------------------------------------------------
# 登录 / 登出 / 当前用户
# ---------------------------------------------------------------------------

@api_bp.post("/login")
def login():
    body = get_json_body()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        return fail(CODE_BAD_PARAM, "用户名和密码不能为空")
    user = query_one("SELECT * FROM users WHERE username=?", (username,))
    # 用户不存在与密码错误统一提示？教学平台保留区分（与 demo-mall 同款），
    # 便于演示"业务码断言"抓文案变化
    if not user:
        return fail(1001, "用户不存在")
    if not verify_password(password, user["password_hash"]):
        return fail(1002, "密码错误")
    from flask import session
    session["user_id"] = user["id"]
    return ok({"id": user["id"], "username": user["username"]})


@api_bp.post("/logout")
def logout():
    from flask import session
    session.clear()
    return ok()


@api_bp.get("/me")
@api_login_required
def me():
    from app.auth import current_user
    user = current_user()
    return ok({"id": user["id"], "username": user["username"]})


# ---------------------------------------------------------------------------
# 项目
# ---------------------------------------------------------------------------

@api_bp.get("/projects")
@api_login_required
def list_projects():
    """项目列表 + 每个项目"用例数 / 最近一次批次结果"（列表页一屏看全）。"""
    rows = query_all(
        "SELECT p.*,"
        " (SELECT COUNT(*) FROM cases c WHERE c.project_id=p.id) AS case_count,"
        " (SELECT e.status FROM executions e WHERE e.project_id=p.id"
        "   ORDER BY e.id DESC LIMIT 1) AS last_status,"
        " (SELECT e.passed FROM executions e WHERE e.project_id=p.id"
        "   ORDER BY e.id DESC LIMIT 1) AS last_passed,"
        " (SELECT e.total  FROM executions e WHERE e.project_id=p.id"
        "   ORDER BY e.id DESC LIMIT 1) AS last_total"
        " FROM projects p ORDER BY p.id")
    data = []
    for r in rows:
        d = dict(r)
        if d["last_total"]:
            d["last_pass_rate"] = round(d["last_passed"] * 100 / d["last_total"], 1)
        else:
            d["last_pass_rate"] = None
        data.append(d)
    return ok(data)


@api_bp.post("/projects")
@api_login_required
def create_project():
    body = get_json_body()
    name = (body.get("name") or "").strip()
    if not name:
        return fail(CODE_BAD_PARAM, "项目名不能为空")
    if query_one("SELECT id FROM projects WHERE name=?", (name,)):
        return fail(CODE_CONFLICT, "项目名已存在")
    pid = db_insert(
        "INSERT INTO projects(name, description, base_url) VALUES(?,?,?)",
        (name, (body.get("description") or "").strip(),
         (body.get("base_url") or "").strip()))
    return ok({"id": pid, "name": name})


@api_bp.get("/projects/<int:project_id>")
@api_login_required
def get_project(project_id):
    row = _get_project_or_fail(project_id)
    if not row:
        return fail(CODE_NOT_FOUND, "项目不存在")
    return ok(dict(row))


@api_bp.put("/projects/<int:project_id>")
@api_login_required
def update_project(project_id):
    row = _get_project_or_fail(project_id)
    if not row:
        return fail(CODE_NOT_FOUND, "项目不存在")
    body = get_json_body()
    name = (body.get("name") or row["name"]).strip()
    exists = query_one("SELECT id FROM projects WHERE name=? AND id<>?", (name, project_id))
    if exists:
        return fail(CODE_CONFLICT, "项目名已存在")
    db_execute(
        "UPDATE projects SET name=?, description=?, base_url=? WHERE id=?",
        (name,
         (body.get("description") if body.get("description") is not None
          else row["description"]),
         (body.get("base_url") if body.get("base_url") is not None
          else row["base_url"]),
         project_id))
    return ok(dict(_get_project_or_fail(project_id)))


@api_bp.delete("/projects/<int:project_id>")
@api_login_required
def delete_project(project_id):
    if not _get_project_or_fail(project_id):
        return fail(CODE_NOT_FOUND, "项目不存在")
    # cases/executions 靠 schema 的 ON DELETE CASCADE 连带删除（连接里已开外键）
    db_execute("DELETE FROM projects WHERE id=?", (project_id,))
    return ok()


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

@api_bp.get("/projects/<int:project_id>/cases")
@api_login_required
def list_cases(project_id):
    """用例列表，附带每条用例"最近一次执行结果"（列表页徽标的数据源）。

    "最近一次"的 SQL 写法：每个 case_id 取 MAX(id) 的 execution_item——
    item 自增 id 越大越新，比按时间字符串排序可靠（同秒会并列）。
    """
    if not _get_project_or_fail(project_id):
        return fail(CODE_NOT_FOUND, "项目不存在")
    rows = query_all(
        "SELECT c.*,"
        " ei.passed AS last_passed, ei.status_code AS last_status_code,"
        " ei.execution_id AS last_execution_id"
        " FROM cases c"
        " LEFT JOIN execution_items ei ON ei.id IN ("
        "   SELECT MAX(id) FROM execution_items GROUP BY case_id)"
        "  AND ei.case_id = c.id"
        " WHERE c.project_id=? ORDER BY c.id", (project_id,))
    return ok([_case_out(r) for r in rows])


@api_bp.post("/projects/<int:project_id>/cases")
@api_login_required
def create_case(project_id):
    if not _get_project_or_fail(project_id):
        return fail(CODE_NOT_FOUND, "项目不存在")
    case, err = importer.normalize_case(get_json_body())
    if err:
        return fail(CODE_BAD_PARAM, err)
    case["name"] = importer.unique_case_name(project_id, case["name"])
    cid = db_insert(
        "INSERT INTO cases(project_id, name, method, path, headers, params, body,"
        " auth_user, auth_pass, expect_status, expect_code, expect_fields)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (project_id, case["name"], case["method"], case["path"], case["headers"],
         case["params"], case["body"], case["auth_user"], case["auth_pass"],
         case["expect_status"], case["expect_code"], case["expect_fields"]))
    return ok({"id": cid})


@api_bp.get("/cases/<int:case_id>")
@api_login_required
def get_case(case_id):
    row = _get_case_or_fail(case_id)
    if not row:
        return fail(CODE_NOT_FOUND, "用例不存在")
    return ok(_case_out(row))


@api_bp.put("/cases/<int:case_id>")
@api_login_required
def update_case(case_id):
    row = _get_case_or_fail(case_id)
    if not row:
        return fail(CODE_NOT_FOUND, "用例不存在")
    case, err = importer.normalize_case(get_json_body())
    if err:
        return fail(CODE_BAD_PARAM, err)
    # 改名也可能撞名：跳过"自己"
    dup = query_one("SELECT id FROM cases WHERE project_id=? AND name=? AND id<>?",
                    (row["project_id"], case["name"], case_id))
    if dup:
        return fail(CODE_CONFLICT, "同项目下已有同名用例")
    db_execute(
        "UPDATE cases SET name=?, method=?, path=?, headers=?, params=?, body=?,"
        " auth_user=?, auth_pass=?, expect_status=?, expect_code=?, expect_fields=?,"
        " updated_at=datetime('now','localtime') WHERE id=?",
        (case["name"], case["method"], case["path"], case["headers"],
         case["params"], case["body"], case["auth_user"], case["auth_pass"],
         case["expect_status"], case["expect_code"], case["expect_fields"], case_id))
    return ok({"id": case_id})


@api_bp.delete("/cases/<int:case_id>")
@api_login_required
def delete_case(case_id):
    if not _get_case_or_fail(case_id):
        return fail(CODE_NOT_FOUND, "用例不存在")
    db_execute("DELETE FROM cases WHERE id=?", (case_id,))
    return ok()


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def _run_and_respond(execution_id: int, case_rows: list[dict], base_url: str):
    summary = runner.execute_batch(
        execution_id, case_rows, base_url,
        concurrency=current_app.config["EXECUTION_CONCURRENCY"])
    return ok({"execution_id": execution_id, **summary})


@api_bp.post("/projects/<int:project_id>/execute")
@api_login_required
def execute_project(project_id):
    """一键执行整个项目（同步阻塞：v1 用例量级下秒级完成，异步化见 docs/05）。"""
    project = _get_project_or_fail(project_id)
    if not project:
        return fail(CODE_NOT_FOUND, "项目不存在")
    rows = query_all("SELECT * FROM cases WHERE project_id=? ORDER BY id", (project_id,))
    if not rows:
        return fail(CODE_BAD_PARAM, "项目下没有用例")
    execution_id = db_insert(
        "INSERT INTO executions(project_id, status, total) VALUES(?,?,?)",
        (project_id, "running", len(rows)))
    return _run_and_respond(execution_id, [dict(r) for r in rows], project["base_url"])


@api_bp.post("/cases/<int:case_id>/execute")
@api_login_required
def execute_single_case(case_id):
    """执行单条用例。也走 executions/execution_items 落库——
    这样"用例列表页的最近结果徽标"与"批次详情页"共用同一份数据，口径统一。"""
    row = _get_case_or_fail(case_id)
    if not row:
        return fail(CODE_NOT_FOUND, "用例不存在")
    project = _get_project_or_fail(row["project_id"])
    execution_id = db_insert(
        "INSERT INTO executions(project_id, status, total) VALUES(?,?,?)",
        (row["project_id"], "running", 1))
    return _run_and_respond(execution_id, [dict(row)], project["base_url"])


@api_bp.get("/executions")
@api_login_required
def list_executions():
    project_id = request.args.get("project_id", type=int)
    if project_id:
        rows = query_all(
            "SELECT e.*, p.name AS project_name FROM executions e"
            " JOIN projects p ON p.id=e.project_id"
            " WHERE e.project_id=? ORDER BY e.id DESC", (project_id,))
    else:
        rows = query_all(
            "SELECT e.*, p.name AS project_name FROM executions e"
            " JOIN projects p ON p.id=e.project_id ORDER BY e.id DESC")
    return ok([_execution_out(r) for r in rows])


@api_bp.get("/executions/<int:execution_id>")
@api_login_required
def get_execution(execution_id):
    row = query_one(
        "SELECT e.*, p.name AS project_name, p.base_url FROM executions e"
        " JOIN projects p ON p.id=e.project_id WHERE e.id=?", (execution_id,))
    if not row:
        return fail(CODE_NOT_FOUND, "执行记录不存在")
    items = query_all(
        "SELECT * FROM execution_items WHERE execution_id=? ORDER BY id",
        (execution_id,))
    data = _execution_out(row)
    data["items"] = [_item_out(i) for i in items]
    return ok(data)


# ---------------------------------------------------------------------------
# 统计 / 导入
# ---------------------------------------------------------------------------

@api_bp.get("/stats")
@api_login_required
def stats():
    """全局统计：计数 + 最近一次批次的通过率（导航/统计页共用）。"""
    counts = {
        "projects": query_one("SELECT COUNT(*) AS c FROM projects")["c"],
        "cases": query_one("SELECT COUNT(*) AS c FROM cases")["c"],
        "executions": query_one("SELECT COUNT(*) AS c FROM executions")["c"],
    }
    last = query_one(
        "SELECT id, project_id, status, total, passed, failed, duration_ms, created_at"
        " FROM executions ORDER BY id DESC LIMIT 1")
    last_out = None
    if last:
        last_out = _execution_out(last)
    recent = query_all(
        "SELECT e.*, p.name AS project_name FROM executions e"
        " JOIN projects p ON p.id=e.project_id ORDER BY e.id DESC LIMIT 10")
    return ok({
        **counts,
        "last_execution": last_out,
        "recent": [_execution_out(r) for r in recent],
    })


@api_bp.post("/import")
@api_login_required
def import_cases():
    """接收平台格式 payload 批量导入（scripts/import_demo_mall.py 的入口）。"""
    data = get_json_body()
    if not isinstance(data, dict) or not data.get("cases"):
        return fail(CODE_BAD_PARAM, "payload 需包含 cases 数组")
    try:
        result = importer.import_payload(data, source="api")
    except ValueError as exc:
        return fail(CODE_BAD_PARAM, str(exc))
    return ok(result)
