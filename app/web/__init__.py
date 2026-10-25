"""页面蓝图：Jinja 服务端渲染的各个页面路由。

页面清单（服务端渲染，数据在路由里查好传给模板）：
    GET/POST /login                        登录
    GET      /logout                       退出
    GET      /projects                     项目列表
    GET      /projects/<id>/cases          用例列表（徽标+一键执行）
    GET      /projects/<id>/executions     批次列表（按项目过滤）
    GET      /executions                   批次列表（全部）
    GET      /executions/<id>              批次详情（<details> 折叠）
    GET/POST /cases/new                    新建用例
    GET/POST /cases/<id>/edit              编辑用例
    GET      /stats                        统计页

为什么页面数据由路由直接查库、而不是前端再调一遍 /api/*：
- 服务端渲染的价值就是"一次请求把 HTML 带数据吐出来"，首屏无等待；
- /api/* 的存在是为了浏览器里的交互动作（执行/删除/新建项目）和脚本调用，
  两条路各自服务各自场景，只有"校验/归一化"逻辑（importer.normalize_case）
  是共享的，避免两份实现漂移。
"""

from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, session, url_for)

from app import importer
from app.auth import current_user, login_required, verify_password
from app.db import execute as db_execute
from app.db import insert as db_insert
from app.db import query_all, query_one

web_bp = Blueprint("web", __name__)


# ---------------------------------------------------------------------------
# 登录 / 退出
# ---------------------------------------------------------------------------

@web_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = query_one("SELECT * FROM users WHERE username=?", (username,))
        if user and verify_password(password, user["password_hash"]):
            session["user_id"] = user["id"]
            return redirect(url_for("web.projects"))
        # 统一模糊提示：登录页不区分"用户不存在/密码错误"（防撞库的惯例）
        flash("用户名或密码错误")
    return render_template("login.html")


@web_bp.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("web.login"))


# ---------------------------------------------------------------------------
# 项目 / 用例 / 执行 页面
# ---------------------------------------------------------------------------

@web_bp.get("/")
@login_required
def index():
    return redirect(url_for("web.projects"))


@web_bp.get("/projects")
@login_required
def projects():
    rows = query_all(
        "SELECT p.*,"
        " (SELECT COUNT(*) FROM cases c WHERE c.project_id=p.id) AS case_count,"
        " (SELECT e.id FROM executions e WHERE e.project_id=p.id"
        "   ORDER BY e.id DESC LIMIT 1) AS last_execution_id,"
        " (SELECT e.status FROM executions e WHERE e.project_id=p.id"
        "   ORDER BY e.id DESC LIMIT 1) AS last_status,"
        " (SELECT e.passed FROM executions e WHERE e.project_id=p.id"
        "   ORDER BY e.id DESC LIMIT 1) AS last_passed,"
        " (SELECT e.total FROM executions e WHERE e.project_id=p.id"
        "   ORDER BY e.id DESC LIMIT 1) AS last_total"
        " FROM projects p ORDER BY p.id")
    return render_template("projects.html", projects=rows)


def _latest_items_for_project(project_id: int) -> dict:
    """每条用例最近一次执行结果（徽标数据源），与 API 端同一口径。

    子查询取每个 case_id 的 MAX(execution_items.id)——自增 id 越大越新；
    再按项目过滤。返回 {case_id: item行}。
    """
    rows = query_all(
        "SELECT ei.* FROM execution_items ei"
        " WHERE ei.id IN (SELECT MAX(id) FROM execution_items GROUP BY case_id)"
        " AND ei.case_id IN (SELECT id FROM cases WHERE project_id=?)",
        (project_id,))
    return {r["case_id"]: r for r in rows}


@web_bp.get("/projects/<int:project_id>/cases")
@login_required
def cases(project_id):
    project = query_one("SELECT * FROM projects WHERE id=?", (project_id,))
    if not project:
        abort(404)
    rows = query_all(
        "SELECT * FROM cases WHERE project_id=? ORDER BY id", (project_id,))
    latest = _latest_items_for_project(project_id)
    last_exec = query_one(
        "SELECT * FROM executions WHERE project_id=? ORDER BY id DESC LIMIT 1",
        (project_id,))
    return render_template("cases.html", project=project, cases=rows,
                           latest=latest, last_exec=last_exec)


@web_bp.get("/projects/<int:project_id>/executions")
@login_required
def project_executions(project_id):
    project = query_one("SELECT * FROM projects WHERE id=?", (project_id,))
    if not project:
        abort(404)
    rows = query_all(
        "SELECT * FROM executions WHERE project_id=? ORDER BY id DESC", (project_id,))
    return render_template("executions.html", project=project, executions=rows)


@web_bp.get("/executions")
@login_required
def all_executions():
    rows = query_all(
        "SELECT e.*, p.name AS project_name FROM executions e"
        " JOIN projects p ON p.id=e.project_id ORDER BY e.id DESC")
    return render_template("executions.html", project=None, executions=rows)


@web_bp.get("/executions/<int:execution_id>")
@login_required
def execution_detail(execution_id):
    execution = query_one(
        "SELECT e.*, p.name AS project_name, p.base_url FROM executions e"
        " JOIN projects p ON p.id=e.project_id WHERE e.id=?", (execution_id,))
    if not execution:
        abort(404)
    items = query_all(
        "SELECT * FROM execution_items WHERE execution_id=? ORDER BY id",
        (execution_id,))
    # 快照/断言在路由里统一解析成对象，模板保持纯展示（模板里写 json.loads 是反模式）
    from app.api import _item_out  # 复用 API 层的解析函数，避免两份实现
    item_dicts = [_item_out(i) for i in items]
    return render_template("execution_detail.html", execution=execution, items=item_dicts)


@web_bp.get("/stats")
@login_required
def stats_page():
    counts = {
        "projects": query_one("SELECT COUNT(*) AS c FROM projects")["c"],
        "cases": query_one("SELECT COUNT(*) AS c FROM cases")["c"],
        "executions": query_one("SELECT COUNT(*) AS c FROM executions")["c"],
        "cases_passed": query_one(
            "SELECT COUNT(*) AS c FROM execution_items WHERE passed=1")["c"],
        "cases_total": query_one("SELECT COUNT(*) AS c FROM execution_items")["c"],
    }
    last_exec = query_one(
        "SELECT e.*, p.name AS project_name FROM executions e"
        " JOIN projects p ON p.id=e.project_id ORDER BY e.id DESC LIMIT 1")
    recent = query_all(
        "SELECT e.*, p.name AS project_name FROM executions e"
        " JOIN projects p ON p.id=e.project_id ORDER BY e.id DESC LIMIT 10")
    return render_template("stats.html", counts=counts,
                           last_exec=last_exec, recent=recent)


# ---------------------------------------------------------------------------
# 用例新建 / 编辑（表单提交走页面路由；校验复用 importer.normalize_case）
# ---------------------------------------------------------------------------

def _case_payload_from_form(form) -> tuple[dict | None, str | None]:
    """表单字段 -> importer.normalize_case 的入参。空文本转 None 交给归一化兜底。"""
    def field(name):
        return (form.get(name) or "").strip() or None

    payload = {
        "name": form.get("name"),
        "method": form.get("method") or "GET",
        "path": form.get("path"),
        "headers": field("headers"),
        "params": field("params"),
        "body": field("body"),
        "auth_user": form.get("auth_user") or "",
        "auth_pass": form.get("auth_pass") or "",
        "expect_status": field("expect_status"),
        "expect_code": field("expect_code"),
        "expect_fields": field("expect_fields"),
    }
    return importer.normalize_case(payload)


@web_bp.route("/cases/new", methods=["GET", "POST"])
@login_required
def case_new():
    project_id = request.args.get("project_id", type=int) \
        or request.form.get("project_id", type=int)
    project = query_one("SELECT * FROM projects WHERE id=?", (project_id,))
    if not project:
        abort(404)
    if request.method == "POST":
        case, err = _case_payload_from_form(request.form)
        if err:
            flash(f"保存失败：{err}")
        else:
            case["name"] = importer.unique_case_name(project_id, case["name"])
            db_insert(
                "INSERT INTO cases(project_id, name, method, path, headers, params,"
                " body, auth_user, auth_pass, expect_status, expect_code,"
                " expect_fields) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, case["name"], case["method"], case["path"],
                 case["headers"], case["params"], case["body"], case["auth_user"],
                 case["auth_pass"], case["expect_status"], case["expect_code"],
                 case["expect_fields"]))
            flash("用例已创建")
            return redirect(url_for("web.cases", project_id=project_id))
    return render_template("case_form.html", project=project, case=None)


@web_bp.route("/cases/<int:case_id>/edit", methods=["GET", "POST"])
@login_required
def case_edit(case_id):
    row = query_one("SELECT * FROM cases WHERE id=?", (case_id,))
    if not row:
        abort(404)
    project = query_one("SELECT * FROM projects WHERE id=?", (row["project_id"],))
    if request.method == "POST":
        case, err = _case_payload_from_form(request.form)
        if err:
            flash(f"保存失败：{err}")
        else:
            dup = query_one(
                "SELECT id FROM cases WHERE project_id=? AND name=? AND id<>?",
                (row["project_id"], case["name"], case_id))
            if dup:
                flash("保存失败：同项目下已有同名用例")
            else:
                db_execute(
                    "UPDATE cases SET name=?, method=?, path=?, headers=?, params=?,"
                    " body=?, auth_user=?, auth_pass=?, expect_status=?,"
                    " expect_code=?, expect_fields=?,"
                    " updated_at=datetime('now','localtime') WHERE id=?",
                    (case["name"], case["method"], case["path"], case["headers"],
                     case["params"], case["body"], case["auth_user"],
                     case["auth_pass"], case["expect_status"], case["expect_code"],
                     case["expect_fields"], case_id))
                flash("用例已保存")
                return redirect(url_for("web.cases", project_id=row["project_id"]))
    # 编辑态把 JSON 文本解析回对象再格式化，方便 textarea 里阅读
    import json as jsonlib
    case = dict(row)
    for field in ("headers", "params", "body", "expect_fields"):
        raw = case.get(field)
        try:
            case[field] = jsonlib.dumps(jsonlib.loads(raw), ensure_ascii=False, indent=2) \
                if raw else ""
        except ValueError:
            pass
    return render_template("case_form.html", project=project, case=case)
