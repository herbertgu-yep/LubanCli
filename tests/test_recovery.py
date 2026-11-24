import asyncio
import json
import sys

import pytest

from lubancli.context import Builder
from lubancli.service import Service
from lubancli.state import Store


async def test_resume_keeps_completed_calls(harness):
    svc, replies, requests, events = harness
    received = asyncio.Event()
    original = svc.notify

    async def pause(kind, text):
        await original(kind, text)
        if kind == "result":
            received.set()
            await asyncio.Future()

    svc.notify = pause
    replies.append(("create_file", {"path": "created", "content": "original"}))
    task = asyncio.create_task(svc.submit("创建文件后确认结果"))
    await asyncio.wait_for(received.wait(), 4)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    svc.notify = original
    svc.resume(svc.sid)
    replies.append("磁盘写入已完成")
    assert await svc.submit("/continue") == "磁盘写入已完成"
    assert (
        svc.s.db.execute("SELECT count(*) FROM run WHERE sid=?", (svc.sid,)).fetchone()[
            0
        ]
        == 1
    )
    assert requests[-1]["messages"][-1]["role"] == "tool"
    assert "original" in requests[-1]["messages"][-1]["content"]
    assert len([k for k, t in events if k == "permission"]) == 1


async def test_changed_files_invalidate_observations(harness):
    svc, replies, requests, _ = harness
    f = svc.s.root / "a"
    f.write_text("obsolete version")
    replies.extend([("read_file", {"path": "a"}), "已读取"])
    await svc.submit("检查版本")
    f.write_text("fresh")
    assert "旧文件观察已失效" in svc.resume(svc.sid)
    replies.append("请重新读取")
    await svc.submit("继续")
    assert "obsolete version" not in str(requests[-1]["messages"])
    assert "obsolete version" in await svc.submit("/history")


async def test_memory_copy_and_bounded_budget(harness, tmp_path):
    svc, _, _, _ = harness
    key = await svc.submit("/memory add project always 使用 Alembic")
    other = tmp_path / "destination"
    other.mkdir()
    b = Service(Store(other, svc.s.home), svc.approve, svc.notify)
    copied = await b.submit("/memory copy " + key)
    await b.submit("/memory edit " + copied + " 使用别的约定")
    assert "Alembic" in await svc.submit("/memory")
    assert copied != key
    p = svc.s.profile().model_copy(update={"window": 1600, "output": 200})
    with pytest.raises(ValueError, match="当前请求"):
        Builder(svc.s, svc.sid).build(p, "附件" * 1200, [], [])


async def test_mcp_via_user_commands(harness):
    from pathlib import Path

    svc, replies, requests, events = harness
    example = Path(__file__).parents[1] / "src/lubancli/example_mcp.py"
    await svc.submit(f'/mcp add syntax "{sys.executable}" "{example}"')
    assert await svc.submit("/mcp tools") == "[]"
    await svc.submit("/mcp trust syntax")
    assert "mcp__syntax__check_syntax" in await svc.submit("/mcp tools")
    replies.extend([("mcp__syntax__check_syntax", {"source": "x=1"}), "语法通过"])
    await svc.submit("用扩展检查语法")
    assert "true" in requests[-1]["messages"][-1]["content"].lower()
    assert any(k == "permission" and "external" in text for k, text in events)
    await svc.submit("/mcp stop syntax")
    assert await svc.submit("/mcp tools") == "[]"


async def test_long_tool_chain_keeps_quoted_constraint(harness):
    svc, replies, requests, events = harness
    svc.s.store_profile(
        svc.s.profile().model_copy(update={"window": 18000, "calls": 100})
    )
    (svc.s.root / "sample.txt").write_text("line\n" * 50)
    replies.extend(
        [
            ("pin_constraint", {"quote": "保持旧配置兼容"}),
            (
                "command",
                {
                    "command": "python3 -c \"print('log line\\\\n' * 12000); print('VERIFIED')\""
                },
            ),
        ]
    )
    replies.extend(
        ("read_file", {"path": "sample.txt", "start": i, "count": 1})
        for i in range(1, 31)
    )
    replies.append("第一轮完成")
    await svc.submit("保持旧配置兼容，检查这些记录。")
    assert "保持旧配置兼容" in str(requests[-1]["messages"])
    assert len(requests) == 33
    assert str(requests[-1]["messages"]).count("log line") < 500
    finished = svc.s.db.execute(
        "SELECT result FROM checkpoint WHERE name='command'"
    ).fetchone()[0]
    assert json.loads(finished)["stdout"].splitlines().count("log line") == 12000
    svc.s.store_profile(svc.s.profile().model_copy(update={"window": 13500}))
    replies.append("继续验证")
    await svc.submit("继续")
    assert "保持旧配置兼容" in str(requests[-1]["messages"])
    assert "VERIFIED" in str(requests[-1]["messages"])
    assert json.loads(await svc.submit("/context"))["summary_until"] > 0


async def test_forget_excludes_old_summary(harness):
    svc, *_ = harness
    key = await svc.submit("/memory add project always 使用旧端口")
    for rid in ["early", "latest"]:
        svc.s.db.execute("INSERT INTO run VALUES(?,?,?)", (rid, svc.sid, "complete"))
        svc.s.log(svc.sid, rid, "user", "使用旧端口")
    await svc.submit("/compact")
    await svc.submit("/memory edit " + key + " 使用新端口")
    ctx = Builder(svc.s, svc.sid).build(svc.s.profile(), "启动服务", [], [])
    assert "使用旧端口" not in str(ctx.messages) and "使用新端口" in str(ctx.messages)
    assert "使用旧端口" in await svc.submit("/history")


async def test_skill_is_selected_from_metadata(harness):
    svc, replies, requests, _ = harness
    replies.extend([("load_skill", {"name": "review"}), "已核对改动"])
    await svc.submit("请审查当前改动")
    assert "review" in str(requests[0]["tools"])
    assert "每条发现附相对路径" not in str(requests[0]["messages"])
    assert "每条发现附相对路径" in requests[1]["messages"][-1]["content"]
    assert json.loads(await svc.submit("/skills"))["last_run"] == "review"
