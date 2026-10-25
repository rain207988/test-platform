"""test-platform 全局配置。

为什么单独一个 config.py（与 demo-mall 同一风格）：
1. 端口、并发数、超时这类"会调的数"不散落在代码里，改一处即可生效；
2. tests/conftest.py 用 create_app({"DB_PATH": ...}) 覆盖部分配置，
   其余默认值仍从这里读——配置的"默认值"与"运行时覆盖"分层清晰。
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---- HTTP 服务 ----
HOST = "127.0.0.1"
PORT = 8000

# session cookie 签名密钥（仅演示用；生产环境从环境变量注入，绝不硬编码入库）
SECRET_KEY = "test-platform-secret-key-change-me"

# ---- 存储 ----
# SQLite 单文件库放 instance/ 目录（Flask 惯例：运行期可变数据与代码分离，且方便 .gitignore）
DB_PATH = os.path.join(BASE_DIR, "instance", "test_platform.db")

# ---- 执行引擎 ----
# 一批用例并发执行时的最大线程数。用例间无共享状态，理论上调大就更快，
# 但被测环境扛不住高并发时会把"用例失败"误判成"被测系统慢"，5 是教学场景的稳妥默认值
EXECUTION_CONCURRENCY = 5

# 单条用例发请求的等待上限（秒）：既是网络超时，也是"断言前拿到响应"的等待上限。
# 超时不算崩溃——execute_case 会把它转成一条失败断言明细，批次继续跑
REQUEST_TIMEOUT = 10

# 自动登录拿到的 token 进程内缓存时长（秒）。
# demo-mall 的 token 有效期 1800 秒，这里取 1500 留 5 分钟余量，
# 避免"缓存里还剩 10 秒过期 → 请求发出时恰好 401"的边界问题
TOKEN_TTL_SECONDS = 1500

# 请求/响应快照里响应体最长保留字符数——防止大响应把数据库和报告页面撑爆
SNAPSHOT_BODY_LIMIT = 4000
