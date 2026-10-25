"""YAML 导入器：把平台自有格式的用例定义批量入库。

平台自有 YAML 格式（与 api-test-framework 的 data/*.yaml 是"映射"关系，见 docs/04）：

    project:
      name: Demo商城接口
      base_url: http://127.0.0.1:5000
      description: 可选
    cases:
      - name: 商品列表默认第一页
        method: GET
        path: /api/goods
        params: {page: 1}
        expect_code: 0
        expect_fields: {"$.data.total": 10}

重名策略：同一项目内用例名唯一（报告/徽标按名字对齐更直观）。
导入遇到重名不报错也不覆盖——自动加后缀 -2/-3…。
"不覆盖"是刻意决策：批量导入最常见的场景是"补量"而不是"改定义"，
覆盖会悄悄毁掉用户手工调整过的用例；要改定义走编辑页。
"""

import json

from app.db import insert as db_insert
from app.db import query_one
from app.db import execute as db_execute

ALLOWED_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"}


def _to_json_text(value, field_name: str):
    """字段归一化：接受 dict/list（YAML 原生）或 JSON 字符串，统一存成 JSON 文本。

    返回 (json_text, error)。空值统一存空串 ''（schema 的 NOT NULL DEFAULT ''）。
    为什么不存原始 YAML 文本：执行引擎 _load_json_field 只认 JSON，
    入库前统一序列化，读取路径就不用兼容两种格式。
    """
    if value is None or value == "" or value == {}:
        return "", None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None, f"{field_name} 不是合法 JSON"
        # 字符串形式也重新序列化一遍，保证存储格式统一（无多余空格）
        return json.dumps(parsed, ensure_ascii=False), None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False), None
    return None, f"{field_name} 只能是对象/数组"


def _to_int_or_none(value, field_name: str):
    """期望值字段：接受 int 或数字字符串，其余报错。None/空 = 该层不断言。"""
    if value is None or value == "":
        return None, None
    try:
        return int(value), None
    except (TypeError, ValueError):
        return None, f"{field_name} 必须是整数"


def normalize_case(payload: dict) -> tuple[dict | None, str | None]:
    """把外部输入的用例定义归一化成可入库的 dict；非法返回 (None, 原因)。

    平台 API（POST /api/projects/<id>/cases）、表单提交（web 蓝图）、
    YAML 导入三条入口共用这一个校验函数——校验逻辑只有一份才不会漂移。
    """
    if not isinstance(payload, dict):
        return None, "用例定义必须是键值对象"
    name = (payload.get("name") or "").strip()
    if not name:
        return None, "name 不能为空"
    method = (payload.get("method") or "GET").strip().upper()
    if method not in ALLOWED_METHODS:
        return None, f"method 不支持：{method}"
    path = (payload.get("path") or "").strip()
    if not path.startswith("/"):
        return None, "path 必须以 / 开头（相对项目 base_url）"

    case = {
        "name": name,
        "method": method,
        "path": path,
        "auth_user": (payload.get("auth_user") or "").strip(),
        "auth_pass": payload.get("auth_pass") or "",
    }

    for field in ("headers", "params", "body"):
        text, err = _to_json_text(payload.get(field), field)
        if err:
            return None, err
        # headers/params/body 只能是对象；body 允许数组吗？JSON body 允许数组，
        # 严格起见 headers/params 必须是对象，body 对象/数组都行
        parsed = json.loads(text) if text else None
        if field in ("headers", "params") and parsed is not None and not isinstance(parsed, dict):
            return None, f"{field} 必须是键值对象"
        case[field] = text

    for field in ("expect_status", "expect_code"):
        value, err = _to_int_or_none(payload.get(field), field)
        if err:
            return None, err
        case[field] = value

    fields_text, err = _to_json_text(payload.get("expect_fields"), "expect_fields")
    if err:
        return None, err
    parsed = json.loads(fields_text) if fields_text else None
    if parsed is not None and not isinstance(parsed, dict):
        return None, "expect_fields 必须是 {JSONPath: 期望值} 形式的对象"
    case["expect_fields"] = fields_text
    return case, None


def unique_case_name(project_id: int, name: str) -> str:
    """项目内用例名去重：重名自动加后缀 -2/-3/-4…（导入不覆盖既有用例）。"""
    candidate = name
    suffix = 2
    while query_one("SELECT id FROM cases WHERE project_id=? AND name=?",
                    (project_id, candidate)):
        candidate = f"{name}-{suffix}"
        suffix += 1
    return candidate


def get_or_create_project(name: str, base_url: str = "", description: str = "") -> int:
    """按名字取项目，不存在则创建——重复导入同一份 YAML 落到同一个项目。"""
    row = query_one("SELECT id FROM projects WHERE name=?", (name,))
    if row:
        return row["id"]
    return db_insert(
        "INSERT INTO projects(name, description, base_url) VALUES(?,?,?)",
        (name, description or "", base_url or ""))


def import_payload(data: dict, source: str = "") -> dict:
    """导入一份平台格式 payload，返回 {project_id, imported, skipped, errors}。

    跳过策略：单条用例格式非法只跳过该条并记录原因，不中断整批——
    50 条里 1 条写错名字不应该让另外 49 条导不进去。
    """
    project_conf = data.get("project") or {}
    project_name = (project_conf.get("name") or "").strip()
    if not project_name:
        raise ValueError("payload 缺少 project.name")

    project_id = get_or_create_project(
        project_name,
        base_url=(project_conf.get("base_url") or "").strip(),
        description=(project_conf.get("description") or "").strip())

    imported, skipped, errors = 0, 0, []
    case_ids = []
    for raw in data.get("cases") or []:
        case, err = normalize_case(raw)
        if err:
            skipped += 1
            errors.append({"name": (raw or {}).get("name", ""), "reason": err})
            continue
        case["name"] = unique_case_name(project_id, case["name"])
        case_ids.append(db_insert(
            "INSERT INTO cases(project_id, name, method, path, headers, params,"
            " body, auth_user, auth_pass, expect_status, expect_code, expect_fields)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, case["name"], case["method"], case["path"],
             case["headers"], case["params"], case["body"],
             case["auth_user"], case["auth_pass"],
             case["expect_status"], case["expect_code"], case["expect_fields"])))
        imported += 1

    # 每次导入留痕：多久导过一次、导了多少、跳了多少——排查"用例怎么变多了"全靠它
    db_insert(
        "INSERT INTO import_logs(project_id, source, imported, skipped, message)"
        " VALUES(?,?,?,?,?)",
        (project_id, source, imported, skipped,
         "；".join(f"{e['name']}: {e['reason']}" for e in errors)[:500]))
    # 项目已存在且没配 base_url 时，用本次导入的 base_url 补上（不覆盖已配置的环境）
    new_base = (project_conf.get("base_url") or "").strip()
    if new_base:
        db_execute("UPDATE projects SET base_url=? WHERE id=? AND base_url=''",
                   (new_base, project_id))

    return {"project_id": project_id, "imported": imported,
            "skipped": skipped, "errors": errors, "case_ids": case_ids}
