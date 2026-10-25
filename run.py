"""test-platform 启动入口。

    python run.py               # 默认 127.0.0.1:8000
    python run.py --port 8001   # 换端口

threaded=True 必须开：浏览器页面 + fetch 请求经常并发到达，
单线程服务器会串行排队，执行一批用例时页面像卡死一样。
"""

import argparse

import config


def main():
    parser = argparse.ArgumentParser(description="test-platform 轻量级接口测试平台")
    parser.add_argument("--port", type=int, default=config.PORT,
                        help=f"监听端口（默认 {config.PORT}）")
    args = parser.parse_args()

    from app import create_app
    app = create_app()

    print(f"[test-platform] http://{config.HOST}:{args.port} "
          f"（Flask 内置服务器，登录账号 admin/admin123）")
    app.run(host=config.HOST, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
