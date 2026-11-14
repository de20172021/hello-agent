"""示例 MCP 服务（stdio）：验证 HelloAgent 的 MCP 接入是否正常。

用法：
    python examples/mcp_echo_server.py

在 HelloAgent 里添加 MCP 服务时填：
    服务名   demo
    命令     <你的 python.exe 绝对路径>
    参数     <项目绝对路径>/examples/mcp_echo_server.py

兼容 MCP SDK 1.x / 2.x。
"""

try:  # MCP SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - MCP SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore

server = _Server("helloagent-demo")


@server.tool()
def add(a: int, b: int) -> int:
    """把两个整数相加。"""
    return a + b


@server.tool()
def echo(text: str) -> str:
    """原样返回文本，用来测试连通性。"""
    return text


@server.tool()
def system_note(text: str) -> str:
    """把一句话写入工作区的 mcp-note.txt 并返回写入结果。"""
    from pathlib import Path

    target = Path.cwd() / "mcp-note.txt"
    target.write_text(text, encoding="utf-8")
    return f"已写入 {target}"


if __name__ == "__main__":
    server.run("stdio")
