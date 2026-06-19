"""AutoUnipus 入口脚本.

v2 重构后,所有业务逻辑都在 core/ 包内:
- 答案优先走 U校园官方答案接口 + AES 解密(`core.sources.api_source`)
- AI 兜底接口已预留(`core.sources.ai_source`,默认未启用)
- 答案命中后写入 SQLite 缓存,下次直接复用
- 题型 handler 可插拔:单选 / 多选 / 填空 / 选词填空 / 翻译

旧版本的"暴力提交反推 isRight"逻辑保留在 res/fetcher.py(已弃用,仅作参考)。

入口:
    python AutoUnipus.py            # CLI 模式,按 account.json 直接跑
    python AutoUnipus.py --webui    # 启动 Web UI(等价于 python webui_launcher.py)
    python webui_launcher.py        # 直接启动 Web UI
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback

from core import logger
from core.config import AppConfig
from core.runner import run_assist_mode, run_auto_mode


def cli_main() -> None:
    try:
        config = AppConfig.load("account.json")
        err = config.validate()
        if err:
            logger.error(f"account.json 配置不合法: {err}")
            return

        logger.tip("已支持单选 / 多选 / 填空 / 选词填空 / 翻译,后续会持续完善")
        print("===== Runtime Log =====")

        if config.automode:
            logger.system("Automode active.")
            run_auto_mode(config)
            input("按 Enter 退出程序...")
        else:
            logger.system("Assistmode active.")
            run_assist_mode(config)

    except FileNotFoundError as e:
        logger.error(str(e))
    except KeyError as e:
        logger.error(f"配置缺字段: {e}")
        logger.tip("可能是 account.json 的配置出错,请对照 README 检查")
    except Exception as e:
        logger.error(str(e))
        log = traceback.format_exc()
        try:
            with open("log.txt", "w", encoding="utf-8") as doc:
                doc.write(log)
            logger.info("错误日志已保存至: log.txt")
        except Exception:
            pass
        logger.tip("系统出错,要不重启一下?")
    finally:
        time.sleep(1.5)


def main() -> None:
    parser = argparse.ArgumentParser(description="AutoUnipus")
    parser.add_argument(
        "--webui", action="store_true",
        help="启动 Web UI 而不是直接走 CLI 流程",
    )
    args, rest = parser.parse_known_args()

    if args.webui:
        # 把剩余参数转给 webui_launcher
        sys.argv = [sys.argv[0]] + rest
        from webui_launcher import main as webui_main
        webui_main()
        return

    cli_main()


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
