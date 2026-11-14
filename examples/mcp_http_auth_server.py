"""示例 MCP 服务：Streamable HTTP + Bearer 鉴权。

用法：
    set MCP_TOKEN=secret-token-123      # Windows
    export MCP_TOKEN=secret-token-123   # macOS / Linux
    python examples/mcp_http_auth_server.py

然后在「设置 → MCP → 添加服务」里填：
    传输方式：http
    服务地址：http://127.0.0.1:8790/mcp
    访问令牌：secret-token-123

这个文件也是「远程 MCP 需要鉴权」的最小参考实现：
鉴权就是一层 ASGI 中间件，拦截没有携带正确 Authorization 头的请求。
"""

from __future__ import annotations

import os

import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

TOKEN = os.environ.get("MCP_TOKEN", "secret-token-123")
PORT = int(os.environ.get("MCP_PORT", "8790"))

server = MCPServer("authed-demo")


@server.tool()
def whoami() -> str:
    """返回当前登录用户（示例）。"""
    return "alice@example.com"


@server.tool()
def add(a: int, b: int) -> int:
    """把两个整数相加。"""
    return a + b


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):  # noqa: ANN001
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


app = server.streamable_http_app()
app.add_middleware(AuthMiddleware)

if __name__ == "__main__":
    print(f"MCP http 服务: http://127.0.0.1:{PORT}/mcp（Bearer {TOKEN}）")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
