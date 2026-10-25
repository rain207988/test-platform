"""应用工厂 create_app（与 demo-mall 同款模式，教学上刻意保持一致）。

为什么用"应用工厂"而不是模块级 app：
- tests 里要 create_app({"DB_PATH": 临时库}) 实例化隔离环境；
- 将来如需多环境（dev/prod 配置类），工厂是唯一不返工的写法——
  Flask 官方推荐 + 企业标配。
"""

import os

from flask import Flask, jsonify

import config


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config)          # 默认配置来自 config.py
    if test_config:
        app.config.update(test_config)      # 测试按需覆盖（DB_PATH/SECRET_KEY...）

    # session cookie 签名（from_object 不会取小写属性名，这里显式兜底）
    app.secret_key = app.config.get("SECRET_KEY", config.SECRET_KEY)

    # 首次启动自动建表（幂等：schema.sql 全部 IF NOT EXISTS）
    if not os.path.exists(app.config["DB_PATH"]):
        from app.db import init_db
        init_db(app.config["DB_PATH"])

    # 请求结束自动关库连接（g 对象生命周期与请求绑定）
    from app import db
    app.teardown_appcontext(db.close_db)

    # 模板里显示当前用户名用（context_processor：所有模板可用）
    @app.context_processor
    def inject_user():
        from app.auth import current_user
        user = current_user()
        return {"current_username": user["username"] if user else None}

    # 模板里渲染 JSON 快照用的过滤器（execution_detail 页把 dict 高亮展示）
    @app.template_filter("tojson_pretty")
    def tojson_pretty(value):
        import json
        return json.dumps(value, ensure_ascii=False, indent=2)

    # 平台自己的健康检查：CI/监控用它判断平台是否就绪（无需登录）
    @app.get("/api/health")
    def health():
        return jsonify({"code": 0, "message": "ok", "data": {"status": "up"}})

    # ---- 注册蓝图：API 与页面两个业务域 ----
    from app.api import api_bp
    from app.web import web_bp
    app.register_blueprint(api_bp)
    app.register_blueprint(web_bp)

    return app
