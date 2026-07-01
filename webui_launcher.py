"""WebUI 启动脚本.

使用:
    python webui_launcher.py                  # 默认 127.0.0.1:5500
    python webui_launcher.py --port 5050
    python webui_launcher.py --host 0.0.0.0   # 局域网访问(注意安全)
"""
from __future__ import annotations

import argparse
import webbrowser
from threading import Timer

from webui.server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="AutoUnipus WebUI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5500)
    parser.add_argument(
        "--config", default="account.json",
        help="account.json 配置文件路径",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="启动后不自动打开浏览器",
    )
    args = parser.parse_args()

    app = create_app(config_path=args.config)

    url = f"http://{args.host}:{args.port}/"
    print(f"==> AutoUnipus WebUI 已启动: {url}")
    print("    按 Ctrl+C 退出")

    if not args.no_browser:
        Timer(0.6, lambda: webbrowser.open(url)).start()

    # debug=False 防止 reloader 起两个进程把 runner 状态搞乱
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
