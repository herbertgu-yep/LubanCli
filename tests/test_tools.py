import asyncio
import json
import sys
from pathlib import Path

import pytest

from lubancli.extensions import connections, fingerprint, invoke, servers
from lubancli.state import Store, digest
from lubancli.tools import Toolbox


async def allow(*args):
    return "once"


async def quiet(*args):
    pass


@pytest.fixture
def store(tmp_path):
    start = tmp_path / "code"
    start.mkdir()
    s = Store(start, tmp_path / "data")
    yield s
    s.db.close()


async def test_tool_observations(store):
    box = Toolbox(store, store.new(), allow, quiet)
    await box.call("create_file", {"path": "a.py", "content": "print(1)\n"})
    assert "a.py:1" in await box.call("read_file", {"path": "a.py"})
    args = {"path": "a.py", "old": "1", "new": "2", "version": digest(b"print(1)\n")}
    assert "+print(2)" in await box.call("edit_file", args)
    with pytest.raises(ValueError, match="冲突"):
        await box.call("edit_file", args)
    with pytest.raises(PermissionError):
        await box.call("command", {"command": "true"}, True)
    result = json.loads(
        await box.call("command", {"command": "echo error >&2; exit 7"})
    )
    assert result["exit_code"] == 7 and result["stderr"] == "error\n"
    (store.root / ".gitignore").write_text("ignored\n")
    (store.root / "ignored").write_text("needle")
    assert not await box.call("search", {"text": "needle"})
    (store.root / "escape").symlink_to(store.home)
    with pytest.raises(ValueError):
        await box.call("read_file", {"path": "escape/state.sqlite3"})


async def test_cancel_process(store):
    box = Toolbox(store, store.new(), allow, quiet)
    task = asyncio.create_task(
        box.call("command", {"command": "sleep 1 && touch unexpected"})
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(1.1)
    assert not (store.root / "unexpected").exists()


async def test_real_mcp(store):
    folder = store.root / ".luban"
    folder.mkdir()
    server = Path(__file__).parents[1] / "src/lubancli/example_mcp.py"
    (folder / "mcp.json").write_text(
        json.dumps({"demo": {"command": sys.executable, "args": [str(server)]}})
    )
    async with connections(store, quiet) as items:
        assert not items
    store.put(store.wid, "mcp:demo", fingerprint(servers(store)["demo"]))
    async with connections(store, quiet) as items:
        result = await invoke(items, "mcp__demo__check_syntax", {"source": "x ="})
        assert any(
            "false" in item.get("text", "").lower()
            for item in json.loads(result)["content"]
        ), result


async def test_real_mcp_write_envelope(store):
    from lubancli.extensions import lookup_receipt, mcp_schema

    folder = store.root / ".luban"
    folder.mkdir()
    server = store.root / "server.py"
    server.write_text(
        "from mcp.server.fastmcp import FastMCP\n"
        "from typing_extensions import TypedDict\n"
        "class WriteResult(TypedDict):\n"
        "    operation_id: str\n"
        "    status: str\n"
        "mcp = FastMCP('fixture')\n"
        "@mcp.tool()\n"
        "def write(key: str) -> WriteResult:\n"
        "    return {'operation_id': key, 'status': 'succeeded'}\n"
        "@mcp.tool()\n"
        "def lookup(operation_id: str) -> WriteResult:\n"
        "    return {'operation_id': operation_id, 'status': 'succeeded'}\n"
        "@mcp.tool()\n"
        "def reject() -> str:\n"
        "    raise ValueError('fixture failure')\n"
        "mcp.run(transport='stdio')\n"
    )
    (folder / "mcp.json").write_text(
        json.dumps(
            {
                "demo": {
                    "command": sys.executable,
                    "args": [str(server)],
                    "idempotency": {"write": "key"},
                    "receipts": {"write": {"tool": "lookup", "field": "operation_id"}},
                }
            }
        )
    )
    store.put(store.wid, "mcp:demo", fingerprint(servers(store)["demo"]))
    async with connections(store, quiet) as items:
        write = next(
            item
            for item in mcp_schema(items)
            if item["function"]["name"] == "mcp__demo__write"
        )
        assert "key" not in write["function"]["parameters"]["properties"]
        output = json.loads(await invoke(items, "mcp__demo__write", {}, "stable-key"))
        assert output["structuredContent"]["operation_id"] == "stable-key"
        receipt = await lookup_receipt(items["mcp__demo__write"], "stable-key")
        assert (
            receipt
            and json.loads(receipt)["structuredContent"]["status"] == "succeeded"
        )
        with pytest.raises(ValueError, match="报告失败"):
            await invoke(items, "mcp__demo__reject", {})
