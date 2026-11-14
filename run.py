"""HelloAgent 启动入口。

用法：
    python run.py                 # 默认 127.0.0.1:8765，自动打开浏览器
    python run.py --port 9000
    python run.py --host 0.0.0.0 --no-browser
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BANNER = r"""
  ┌─────────────────────────────────┐
  │  HelloAgent · 本地智能体工作台  │
  └─────────────────────────────────┘
"""


def _free_port(host: str, preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host if host != "0.0.0.0" else "", port))
                return port
            except OSError:
                continue
    raise SystemExit(f"端口 {preferred} 起连续 20 个端口都被占用")


def _open_later(url: str, delay: float = 1.2) -> None:
    def worker() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=worker, daemon=True).start()


def _check_env() -> None:
    try:
        import torch  # noqa: F401
    except ImportError:
        print("  [提示] 未检测到 torch：本地模型推理不可用。")
        print("         如需本地推理：pip install -r requirements.txt")
        print("         也可以改用 LM Studio / Ollama 等 OpenAI 兼容服务。\n")
    if sys.version_info < (3, 9):
        raise SystemExit("需要 Python 3.9 及以上版本")


def main() -> None:
    parser = argparse.ArgumentParser(description="HelloAgent —— 本地智能体工作台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--reload", action="store_true", help="开发模式：代码变更自动重载")
    args = parser.parse_args()

    _check_env()
    port = _free_port(args.host, args.port)
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '') else args.host}:{port}"

    print(BANNER)
    print(f"  HelloAgent 已启动 → {url}")
    print(f"  环境: {sys.executable}")
    print("  按 Ctrl+C 停止服务\n")

    if not args.no_browser and not args.reload:
        _open_later(url)

    import uvicorn

    if args.reload:
        uvicorn.run("app.main:app", host=args.host, port=port, reload=True, log_level="info")
    else:
        from app.main import app

        uvicorn.run(app, host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  已停止。")
