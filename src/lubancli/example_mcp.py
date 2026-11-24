"""Read-only stdio server for checking Python syntax without executing code."""

import ast

from mcp.server.fastmcp import FastMCP

server = FastMCP("syntax")


@server.tool()
def check_syntax(source: str) -> dict:
    """Parse supplied Python source and report syntax errors."""
    try:
        ast.parse(source)
    except SyntaxError as exc:
        return {"valid": False, "line": exc.lineno, "message": exc.msg}
    return {"valid": True}


if __name__ == "__main__":
    server.run(transport="stdio")
