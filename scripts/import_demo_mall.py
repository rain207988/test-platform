"""演示脚本：把 api-test-framework/data/*.yaml 的用例批量转换并导入 test-platform。

用法（先启动被测系统 5000 与平台 8000）：
    python scripts/import_demo_mall.py \
        --url http://127.0.0.1:8000 \
        --api-base http://127.0.0.1:5000

导入机制（详见 docs/04-用例管理与YAML导入.md）：
1. 读取 api-test-framework 的 YAML（api 名 + params + expect 的"数据驱动"格式）；
2. 通过内置映射表 API_MAP 把 api 名翻译成 平台格式（method/path/params/body/auth）；
3. 组装平台格式 payload，走平台 REST API（POST /api/import）批量入库。

为什么走平台 REST API 而不是直接写 SQLite（刻意决策）：
- 解耦：脚本只依赖平台的 HTTP 契约，不碰内部 schema/DB 路径——
  平台换机器部署甚至换数据库，脚本一行不用改；
- 狗粮：导入过程本身就是对平台 /api/login、/api/import 的真实集成测试；
- 单一实现：字段校验、重名后缀等逻辑只在平台 importer.py 有一份，
  脚本里再写一份"直接写库"必然随平台演进而漂移。

链路用例（chain_data.yaml，带 steps+extract）会自动跳过并计数：
单条用例=多个步骤+变量提取，属于"多步编排"，是平台 v2 的方向，
v1 的执行引擎按"单请求单断言集"设计（无状态、可并发）。
"""

import argparse
import json
import os
import sys

import requests
import yaml

# 被测系统（demo-mall）的种子账号：需要鉴权的用例执行时由平台自动登录
DEFAULT_MALL_USER = "lemon"
DEFAULT_MALL_PASS = "123456"

# 默认数据目录：api-test-framework 的 data/*.yaml
DEFAULT_DATA_DIR = r"D:\软件测试\api-test-framework\data"

# YAML 文件名 -> 用例名前缀（报告里一眼看出用例属于哪个域）
DOMAIN_LABELS = {
    "login_data.yaml": "认证",
    "goods_data.yaml": "商品",
    "cart_data.yaml": "购物车",
    "order_data.yaml": "订单",
    "chain_data.yaml": "链路",   # 整文件跳过（多步编排，v2 方向）
}

# ---------------------------------------------------------------------------
# api 名 -> 平台用例定义 的映射表（核心：平台不用逐条改 YAML，只维护这一张表）
# 每个条目返回 dict(method, path, kind, need_auth)：
#   kind=query 把 params 放查询串；kind=body 把 params 放请求体；
#   kind=path 表示路径里带资源 id（goods_detail 等）；kind=none 表示无入参
# ---------------------------------------------------------------------------
API_MAP = {
    "login":         {"method": "POST", "path": "/api/auth/login", "kind": "body", "need_auth": False},
    "register":      {"method": "POST", "path": "/api/auth/register", "kind": "body", "need_auth": False},
    "me":            {"method": "GET", "path": "/api/auth/me", "kind": "none", "need_auth": True},
    "me_with_token": {"method": "GET", "path": "/api/auth/me", "kind": "token_header", "need_auth": False},
    "goods_list":    {"method": "GET", "path": "/api/goods", "kind": "query", "need_auth": False},
    "goods_detail":  {"method": "GET", "path": "/api/goods/{goods_id}", "kind": "path", "need_auth": False},
    "cart_get":      {"method": "GET", "path": "/api/cart", "kind": "none", "need_auth": True},
    "cart_add":      {"method": "POST", "path": "/api/cart/items", "kind": "body", "need_auth": True},
    "cart_update":   {"method": "PUT", "path": "/api/cart/items/{item_id}", "kind": "path_body", "need_auth": True},
    "cart_delete":   {"method": "DELETE", "path": "/api/cart/items/{item_id}", "kind": "path", "need_auth": True},
    "order_create":  {"method": "POST", "path": "/api/orders", "kind": "body", "need_auth": True},
    "order_list":    {"method": "GET", "path": "/api/orders", "kind": "query", "need_auth": True},
    "order_detail":  {"method": "GET", "path": "/api/orders/{order_id}", "kind": "path", "need_auth": True},
    "order_pay":     {"method": "POST", "path": "/api/orders/{order_id}/pay", "kind": "path", "need_auth": True},
    "order_cancel":  {"method": "POST", "path": "/api/orders/{order_id}/cancel", "kind": "path", "need_auth": True},
}


def convert_case(api_name: str, params: dict, need_auth: bool) -> dict:
    """单条 YAML 用例的 api/params -> 平台用例的 method/path/params/body/headers。"""
    spec = API_MAP[api_name]
    params = params or {}
    case = {"method": spec["method"], "path": spec["path"], "headers": None,
            "params": None, "body": None}

    kind = spec["kind"]
    if kind == "query":
        case["params"] = params
    elif kind == "body":
        # 注意：即使 params 为空也要发 {}（body 为空 dict 时平台存空串，
        # 请求不带 body——与参考框架 order_create 的 `body or None` 行为对齐）
        case["body"] = params
    elif kind == "path":
        # 资源 id 拼进路径：/api/goods/1；剩余参数本就与路径无关，直接丢弃
        # （参考框架用 f-string 拼路径，语义相同）
        case["path"] = spec["path"].format(**params)
    elif kind == "path_body":
        # 路径带 id + body 还要带其余字段（cart_update：PUT /api/cart/items/{id} + quantity）
        body = {k: v for k, v in params.items() if f"{{{k}}}" not in spec["path"]}
        case["path"] = spec["path"].format(**{k: params.get(k) for k in
                                              ("item_id", "order_id", "goods_id")})
        case["body"] = body or None
    elif kind == "token_header":
        # me_with_token：显式把（伪造的）token 放进 Authorization 头，
        # 专门测"非法 token 被拒绝"——平台约定用例 headers 优先于自动登录
        case["headers"] = {"Authorization": f"Bearer {params.get('token', '')}"}

    if need_auth:
        case["auth_user"] = DEFAULT_MALL_USER
        case["auth_pass"] = DEFAULT_MALL_PASS
    return case


def convert_expect(expect: dict) -> dict:
    """参考框架 expect 三层结构 -> 平台三个期望字段。"""
    expect = expect or {}
    out = {}
    if "status_code" in expect:
        out["expect_status"] = expect["status_code"]
    if "code" in expect:
        out["expect_code"] = expect["code"]
    if expect.get("fields"):
        out["expect_fields"] = expect["fields"]
    return out


def convert_file(path: str, api_base: str) -> tuple[list[dict], int]:
    """转换单个 YAML 文件。返回 (平台用例列表, 跳过的链路用例数)。"""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    domain = DOMAIN_LABELS.get(os.path.basename(path), "通用")
    cases, skipped = [], 0
    for raw in data.get("cases") or []:
        if "steps" in raw:
            skipped += 1      # 链路用例（多步编排）v1 不支持，计入跳过
            continue
        api_name = raw["api"]
        if api_name not in API_MAP:
            skipped += 1
            print(f"  [跳过] 未映射的 api：{api_name}（用例：{raw.get('name')}）")
            continue
        need_auth = API_MAP[api_name]["need_auth"] and not raw.get("no_auth")
        platform_case = {
            "name": f"[{domain}] {raw['name']}",
            **convert_case(api_name, raw.get("params"), need_auth),
            **convert_expect(raw.get("expect")),
        }
        cases.append(platform_case)
    return cases, skipped


def login_platform(url: str, username: str, password: str) -> requests.Session:
    """登录平台拿 session cookie。trust_env=False 的原因见 README 常见坑。"""
    session = requests.Session()
    session.trust_env = False
    resp = session.post(f"{url.rstrip('/')}/api/login",
                        json={"username": username, "password": password}, timeout=10)
    body = resp.json()
    if resp.status_code != 200 or body.get("code") != 0:
        raise SystemExit(f"平台登录失败：HTTP {resp.status_code} {body}")
    print(f"[1/3] 已登录平台 {url}（{username}）")
    return session


def main():
    parser = argparse.ArgumentParser(description="把 api-test-framework 的 YAML 用例导入 test-platform")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="平台地址")
    parser.add_argument("--api-base", default="http://127.0.0.1:5000",
                        help="被测环境地址（写入项目 base_url）")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                        help="api-test-framework 的 data 目录")
    parser.add_argument("--username", default="admin", help="平台登录账号")
    parser.add_argument("--password", default="admin123", help="平台登录密码")
    parser.add_argument("--project-name", default="Demo商城接口",
                        help="导入目标项目（不存在会自动创建）")
    args = parser.parse_args()

    session = login_platform(args.url, args.username, args.password)

    # ---- 转换全部 YAML ----
    total_cases, total_skipped = [], 0
    for filename in sorted(DOMAIN_LABELS):
        path = os.path.join(args.data_dir, filename)
        if not os.path.exists(path):
            print(f"[warn] 找不到 {path}，跳过")
            continue
        cases, skipped = convert_file(path, args.api_base)
        total_cases.extend(cases)
        total_skipped += skipped
        print(f"  {filename}: 转换 {len(cases)} 条，跳过 {skipped} 条")
    print(f"[2/3] 转换完成：待导入 {len(total_cases)} 条，跳过 {total_skipped} 条（链路用例）")

    # ---- 走平台 API 批量导入 ----
    payload = {
        "project": {"name": args.project_name, "base_url": args.api_base,
                    "description": "由 scripts/import_demo_mall.py 从 api-test-framework/data 导入"},
        "cases": total_cases,
    }
    resp = session.post(f"{args.url.rstrip('/')}/api/import", json=payload, timeout=60)
    body = resp.json()
    if resp.status_code != 200 or body.get("code") != 0:
        raise SystemExit(f"导入失败：HTTP {resp.status_code} {json.dumps(body, ensure_ascii=False)}")
    data = body["data"]
    print(f"[3/3] 导入完成：成功 {data['imported']} 条，跳过 {data['skipped']} 条，"
          f"项目 id={data['project_id']}")
    for err in data.get("errors") or []:
        print(f"  [格式非法] {err['name']}: {err['reason']}")
    print(f"打开 {args.url}/projects/{data['project_id']}/cases 查看用例列表")


if __name__ == "__main__":
    main()
