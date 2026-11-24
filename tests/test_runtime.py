import asyncio
import json
import sys

import httpx
import pytest
from openai import RateLimitError

from lubancli import agent
from lubancli.tools import Toolbox


async def test_parallel_reads_ordered_and_shared_hooks(harness, monkeypatch):
    svc, _, _, _ = harness
    for path in ["one", "two"]:
        (svc.s.root / path).write_text(path)
    await svc.submit("/hooks PostToolUse read_file trace")
    arrived = 0
    gate = asyncio.Event()
    original = Toolbox.call

    async def overlap(self, name, *args, **kw):
        nonlocal arrived
        if name == "read_file":
            arrived += 1
            if arrived == 2:
                gate.set()
            await asyncio.wait_for(gate.wait(), 1)
        return await original(self, name, *args, **kw)

    monkeypatch.setattr(Toolbox, "call", overlap)
    count = 0

    async def complete(client, p, messages, schemas, notify):
        nonlocal count
        count += 1
        if count == 2:
            assert [m["tool_call_id"] for m in messages[-2:]] == ["one", "two"]
            return {"role": "assistant", "content": "ok"}
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": k,
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": k}),
                    },
                }
                for k in ["one", "two"]
            ],
        }

    monkeypatch.setattr(agent, "complete", complete)
    assert await svc.submit("read both") == "ok"
    trace = json.loads(await svc.submit("/trace"))
    assert len([e for e in trace if e["event"] == "hook"]) == 2
    assert len([e for e in trace if e["event"] == "tool_timing"]) == 2


async def test_fallback_reassembles_and_charges_usage(harness, monkeypatch):
    svc, _, _, _ = harness
    parent = svc.s.profile()
    svc.s.store_profile(parent.model_copy(update={"name": "small", "window": 22000}))
    svc.s.store_profile(parent.model_copy(update={"fallback": "small", "retries": 0}))
    models = []

    async def complete(client, p, messages, schemas, notify):
        models.append(p.name)
        if p.name == "test":
            raise RateLimitError(
                "busy",
                response=httpx.Response(
                    429, request=httpx.Request("POST", "https://test.invalid")
                ),
                body=None,
            )
        return {
            "role": "assistant",
            "content": "ok",
            "_usage": {
                "prompt_tokens": 60,
                "completion_tokens": 4,
                "prompt_tokens_details": {"cached_tokens": 30},
            },
        }

    monkeypatch.setattr(agent, "complete", complete)
    await svc.submit("hello")
    report = json.loads(await svc.submit("/metrics"))
    assert (
        models == ["test", "small"]
        and report["tokens"] == 64
        and report["cached_input_ratio"] == 0.5
    )
    assert json.loads(await svc.submit("/context"))["window"] == 22000


async def test_readonly_auto_and_dangerous_commands(harness):
    svc, _, _, events = harness
    await svc.submit("/policy readonly")
    with pytest.raises(PermissionError):
        await svc.box.call("command", {"command": "echo nope"})
    await svc.submit("/policy auto")
    await svc.box.call("create_file", {"path": "ok", "content": "a"})
    assert not [k for k, _ in events if k == "permission"]
    with pytest.raises(PermissionError):
        await svc.box.call("command", {"command": "rm -rf unused"})
    await svc.submit("/policy ask")
    await svc.box.call("create_file", {"path": "ok2", "content": "a"})
    assert ("permission", "edit " + str(svc.s.root / "ok2")) in events


async def test_sandbox_backend_fails_closed(harness, monkeypatch, tmp_path):
    svc, _, _, _ = harness
    await svc.submit("/sandbox on")
    monkeypatch.setattr(sys, "platform", "unsupported")
    with pytest.raises(RuntimeError, match="隔离后端"):
        await svc.box.call("command", {"command": "touch unexpected"})
    assert not (svc.s.root / "unexpected").exists()


async def test_delegation_uses_separate_model_and_role_tool_set(harness, monkeypatch):
    svc, _, _, _ = harness
    parent = svc.s.profile()
    child = parent.model_copy(
        update={"name": "scout", "allowed_tools": ["read_file"], "token_limit": 100000}
    )
    svc.s.store_profile(child)
    svc.s.store_profile(parent)
    await svc.submit("/role Explorer scout")
    roles = []

    async def respond(client, p, messages, schemas, notify):
        roles.append(p.name)
        if len(roles) == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "task",
                        "type": "function",
                        "function": {
                            "name": "delegate",
                            "arguments": json.dumps(
                                {
                                    "role": "Explorer",
                                    "goal": "Find the entry point",
                                    "acceptance": "Return one path",
                                }
                            ),
                        },
                    }
                ],
            }
        if p.name == "scout":
            assert [s["function"]["name"] for s in schemas] == ["read_file"]
            return {"role": "assistant", "content": "main.py:1"}
        assert "main.py:1" in str(messages[-1])
        return {"role": "assistant", "content": "main.py:1"}

    monkeypatch.setattr(agent, "complete", respond)
    assert "main.py" in await svc.submit("investigate")
    assert roles == ["test", "scout", "test"]


async def test_token_budget_rejects_before_transport(harness):
    svc, _, requests, _ = harness
    await svc.submit("/runtime token_limit 50")
    with pytest.raises(RuntimeError, match="预算"):
        await svc.submit("explain")
    assert not requests


async def test_sdk_stream_reports_usage_and_sends_cache_hint(harness):
    from openai import AsyncOpenAI

    svc, _, _, _ = harness
    p = svc.s.profile().model_copy(update={"prompt_cache": True, "stream_usage": True})
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        common = {
            "id": "r",
            "model": p.model,
            "created": 1,
            "object": "chat.completion.chunk",
        }
        frames = [
            {
                **common,
                "choices": [
                    {"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}
                ],
            },
            {
                **common,
                "choices": [],
                "usage": {
                    "prompt_tokens": 32,
                    "completion_tokens": 1,
                    "total_tokens": 33,
                    "prompt_tokens_details": {"cached_tokens": 16},
                },
            },
        ]
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text="".join("data: " + json.dumps(x) + "\n\n" for x in frames)
            + "data: [DONE]\n\n",
        )

    async with AsyncOpenAI(
        api_key="fixture",
        base_url=p.base_url,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as sdk:
        answer = await agent.complete(
            sdk,
            p,
            [
                {"role": "system", "content": "same prefix"},
                {"role": "user", "content": "test"},
            ],
            [],
            svc.notify,
        )
    assert answer["_usage"]["prompt_tokens_details"]["cached_tokens"] == 16
    assert sent[0]["prompt_cache_key"] and sent[0]["stream_options"] == {
        "include_usage": True
    }


async def test_native_shell_boundary_when_host_permits(harness, tmp_path):
    import shlex
    import shutil
    import sys

    svc, _, _, _ = harness
    if sys.platform != "darwin" and not (
        sys.platform.startswith("linux") and shutil.which("bwrap")
    ):
        pytest.skip("native sandbox unavailable")
    await svc.submit("/sandbox on")

    async def run(command):
        return json.loads(await svc.box.call("command", {"command": command}))

    smoke = await run("echo checked > inside.txt")
    if smoke["exit_code"] and any(
        msg in smoke["stderr"].lower()
        for msg in (
            "operation not permitted",
            "creating new namespace failed",
            "no permissions to create",
        )
    ):
        pytest.skip("outer host prevents native sandbox creation")
    assert smoke["exit_code"] == 0, smoke["stderr"]
    assert (svc.s.root / "inside.txt").read_text().strip() == "checked"
    outside = tmp_path / "outside.txt"
    assert (await run("echo unexpected > " + shlex.quote(str(outside))))[
        "exit_code"
    ] != 0
    assert not outside.exists()


async def test_pre_hook_denies_before_writing(harness):
    svc, replies, _, _ = harness
    await svc.submit("/hooks PreToolUse create_file deny 请人工处理")
    replies.extend([("create_file", {"path": "denied", "content": "x"}), "已拒绝"])
    await svc.submit("创建文件")
    assert not (svc.s.root / "denied").exists()
    assert "请人工处理" in await svc.submit("/trace")
