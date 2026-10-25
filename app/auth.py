"""平台自身的认证：pbkdf2 密码摘要 + session 登录态 + 两个访问控制装饰器。

为什么平台自己的登录用 session 而不是 JWT（被测系统 demo-mall 用的是 JWT）：
- 平台是"服务端渲染 + 浏览器直接点"的多页应用，session-cookie 是浏览器场景
  最省事的方案：HttpOnly 防 XSS 偷 token、随请求自动携带、注销即失效；
- JWT 的优势在"无状态横向扩展 + 跨服务携带"，单机教学平台一个都没用上；
- 两个装饰器各管一摊：页面类 login_required（未登录 302 到 /login），
  API 类 api_login_required（未登录 401 + {"code":4010}，与被测系统同款约定）。

为什么 pbkdf2 而不是像 demo-mall 那样一层 hmac：
- demo-mall 的 hash 是教学简化版（固定盐单轮摘要）；
- 平台存的是真实管理员密码，按 OWASP 推荐用带随机盐 + 高迭代次数的
  pbkdf2_hmac：盐防彩虹表，迭代次数抬高暴力破解成本。
  生产项目更常用 bcrypt/argon2，原理相同：慢哈希 + 随机盐 + 只存摘要。
"""

import hashlib
import hmac
import secrets

from flask import jsonify, redirect, session, url_for

from app.db import query_one

# 迭代次数：越高越难暴破，也越耗 CPU。12 万次在本机约几十毫秒，登录场景无感
PBKDF2_ITERATIONS = 120_000


def hash_password(password: str) -> str:
    """生成 "pbkdf2$sha256$<iterations>$<salt>$<hash>" 格式的摘要串。

    把算法名/迭代次数/盐一起存进摘要串——以后升级算法或提高迭代次数时，
    verify 能按旧参数校验老密码、按新参数透明重哈希，不需要"全员重置密码"。
    """
    salt = secrets.token_hex(16)  # 每个密码独立随机盐
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return f"pbkdf2$sha256${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """按摘要串里记录的参数重算一遍，恒定时间比较（防时序攻击）。"""
    try:
        _algo, _hash_name, iterations, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False  # 摘要串格式不对一律按"校验失败"处理，不外泄格式信息


def current_user():
    """从 session 取当前登录用户（模板里显示用户名用）。未登录返回 None。"""
    user_id = session.get("user_id")
    if not user_id:
        return None
    return query_one("SELECT id, username FROM users WHERE id = ?", (user_id,))


def login_required(view):
    """页面蓝图专用装饰器：未登录 302 到 /login（浏览器体验优先）。"""
    from functools import wraps

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("web.login"))
        return view(*args, **kwargs)

    return wrapper


def api_login_required(view):
    """API 蓝图专用装饰器：未登录返回 401 + {"code":4010}。

    故意与被测系统 demo-mall 的未登录响应完全同构——这样"用平台测平台"
    （tests/test_web_api.py）的用例写起来和测被测系统一模一样，也方便
    文档里对比讲解"平台自己也是一套被测系统"。
    """
    from functools import wraps

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"code": 4010, "message": "未登录或会话已过期", "data": None}), 401
        return view(*args, **kwargs)

    return wrapper
