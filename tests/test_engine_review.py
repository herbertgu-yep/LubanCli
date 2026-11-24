import asyncio
import json
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult

from lubancli import agent
from lubancli.evaluation import evaluate
from lubancli.extensions import ExternalTool, Receipt, UnknownToolOutcome, invoke
from lubancli.state import digest


async def test_missing_terminal_frame_does_not_dispatch(harness, monkeypatch):
    svc, _, _, events = harness

    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def __aiter__(self):
            tool = SimpleNamespace(
                index=0,
                id="write",
                function=SimpleNamespace(
                    name="create_file", arguments='{"path":"unexpected","content":"x"}'
                ),
            )
            delta = SimpleNamespace(content="partial", tool_calls=[tool])
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=delta, finish_reason=None)]
            )

    async def create(**kwargs):
        return Stream()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def client(*args):
        yield SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

    monkeypatch.setattr(agent, "client_for", client)
    with pytest.raises(agent.IncompleteResponse):
        await svc.submit("create file")
    assert not (svc.s.root / "unexpected").exists()
    assert svc.s.db.execute("SELECT status FROM run").fetchone()[0] == "interrupted"
    assert any(kind == "delta" and text == "partial" for kind, text in events)


async def test_text_only_reply_does_not_claim_verified(harness):
    svc, replies, _, events = harness
    replies.append("已经创建")
    await svc.submit("请创建 file.txt")
    assert not (svc.s.root / "file.txt").exists()
    assert svc.s.db.execute("SELECT status FROM run").fetchone()[0] == "answered"
    assert any(
        kind == "outcome" and "没有记录到文件修改" in text for kind, text in events
    )


async def test_mcp_timeout_stops_run_and_requires_reconciliation(harness, monkeypatch):
    from contextlib import asynccontextmanager

    svc, replies, requests, _ = harness
    attempts = []

    class Client:
        async def call_tool(self, name, args):
            attempts.append(args)
            raise TimeoutError

    item = SimpleNamespace(
        name="write", description="Fixture", inputSchema={"type": "object"}
    )
    items = {"mcp__fixture__write": ExternalTool(Client(), item)}

    @asynccontextmanager
    async def extensions(*args):
        yield items

    monkeypatch.setattr(agent.Agent, "extensions", extensions)
    replies.append(("mcp__fixture__write", {"value": "x"}))
    with pytest.raises(UnknownToolOutcome):
        await svc.submit("write once")
    assert (len(attempts), len(requests)) == (1, 1)
    row = svc.s.db.execute("SELECT status, args FROM checkpoint").fetchone()
    assert row[0] == "unknown" and json.loads(row[1])["operation_id"]
    assert json.loads(row[1])["operation_id"] in await svc.submit("/trace")
    with pytest.raises(ValueError, match="未确认"):
        await svc.submit("/continue")


async def test_mcp_retry_reuses_configured_key_and_error_is_failure():
    calls = []

    class Client:
        async def call_tool(self, name, args):
            calls.append(args)
            if len(calls) == 1:
                raise TimeoutError
            return CallToolResult(content=[], isError=True)

    schema = {"type": "object", "properties": {"key": {"type": "string"}}}
    tool = SimpleNamespace(name="write", inputSchema=schema)
    with pytest.raises(ValueError, match="报告失败"):
        await invoke(
            {"write": ExternalTool(Client(), tool, "key")},
            "write",
            {"key": "model supplied"},
            "stable",
        )
    assert calls == [{"key": "stable"}, {"key": "stable"}]


async def test_resume_skips_completed_roles_and_preserves_completed_edit(harness):
    svc, replies, requests, _ = harness
    await svc.submit("/agents on")

    def interrupted(request):
        raise asyncio.CancelledError

    replies.extend(
        [
            "explored",
            ("create_file", {"path": "result.txt", "content": "once"}),
            "implemented",
            ("read_file", {"path": "result.txt"}),
            interrupted,
        ]
    )
    with pytest.raises(asyncio.CancelledError):
        await svc.submit("create a result")
    revision = (svc.s.root / "result.txt").stat().st_mtime_ns
    svc.resume(svc.sid)
    replies.append("reviewed")
    before = len(requests)
    assert "reviewed" in await svc.submit("/continue")
    assert len(requests) == before + 1
    messages = requests[-1]["messages"]
    assert any(m.get("role") == "tool" and "once" in m["content"] for m in messages)
    assert not any(
        c["function"]["name"] == "create_file"
        for m in messages
        for c in m.get("tool_calls", [])
    )
    assert (svc.s.root / "result.txt").stat().st_mtime_ns == revision


async def test_oversized_attachment_uses_reference_and_resume_marks_change(harness):
    svc, replies, requests, _ = harness
    path = svc.s.root / "large.txt"
    path.write_text(("old evidence " * 128 + "\n") * 100)
    await svc.submit("/runtime allowed_tools read_file")

    def interrupted(request):
        raise asyncio.CancelledError

    replies.append(interrupted)
    with pytest.raises(asyncio.CancelledError):
        await svc.submit("analyze @large.txt:1-100")
    first = json.dumps(requests[0]["messages"], ensure_ascii=False)
    assert "@large.txt:1-100" in first and "old evidence" not in first
    path.write_text("current")
    svc.resume(svc.sid)
    replies.append("needs reread")
    await svc.submit("/continue")
    assert "证据已失效" in json.dumps(requests[-1]["messages"], ensure_ascii=False)
    assert "old evidence" not in json.dumps(requests[-1]["messages"])


async def test_plan_waits_for_evidence_before_starting_dependent_step(harness):
    svc, replies, requests, _ = harness
    plan = {
        "reason": "dependency",
        "steps": [
            {
                "goal": "create first",
                "acceptance": "file exists",
                "evidence_kind": "change",
            },
            {"goal": "create second", "dependencies": [1], "acceptance": "file exists"},
        ],
    }
    svc.s.db.execute(
        "UPDATE session SET plan=? WHERE id=?", (json.dumps(plan), svc.sid)
    )
    (svc.s.root / "notes.txt").write_text("existing")
    replies.extend([("read_file", {"path": "notes.txt"}), "first done"])
    with pytest.raises(agent.PlanBlocked):
        await svc.submit("/plan execute")
    assert len(requests) == 2
    replies.extend(
        [
            ("create_file", {"path": "first", "content": "1"}),
            "first",
            ("create_file", {"path": "second", "content": "2"}),
            "second",
        ]
    )
    await svc.submit("/continue")
    assert (svc.s.root / "first").exists() and (svc.s.root / "second").exists()
    steps = json.loads(svc.s.session(svc.sid)["plan"])["steps"]
    assert [s["status"] for s in steps] == ["done", "done"]


async def test_oscillation_replans_only_once(harness):
    svc, replies, requests, events = harness
    (svc.s.root / "sample.txt").write_text("red\nblue\n")
    replies.extend(
        (
            "search",
            {"text": key, "pattern": "*.txt"}
            if i % 2
            else {"pattern": "*.txt", "text": key},
        )
        for i, key in enumerate(["red", "blue"] * 6)
    )
    with pytest.raises(RuntimeError, match="重规划后仍无进展"):
        await svc.submit("locate")
    assert len(requests) == 6
    assert "改变调查路径" in json.dumps(requests[-1]["messages"], ensure_ascii=False)
    assert (
        sum(kind == "replan" for (kind,) in svc.s.db.execute("SELECT kind FROM event"))
        == 1
    )


async def test_known_failure_is_not_repaired(harness):
    svc, replies, _, _ = harness
    (svc.s.root / "tests").mkdir()
    (svc.s.root / "tests/test_existing.py").write_text(
        "def test_existing():\n    assert 1 == 0\n"
    )
    await svc.submit("/runtime verification_baseline true")
    replies.extend(
        [("create_file", {"path": "feature.py", "content": "x=1"}), "created"]
    )
    response = await svc.submit("add a feature")
    assert "与修改前一致的失败" in response
    assert svc.s.db.execute("SELECT status FROM run").fetchone()[0] == "changed"


async def test_model_evaluation_grades_real_artifact_with_scripted_transport(harness):
    svc, replies, _, _ = harness
    old = '{"retries": 2, "timeout": 15}\n'
    replies.extend(
        [
            (
                "edit_file",
                {
                    "path": "settings.json",
                    "version": digest(old.encode()),
                    "old": "2,",
                    "new": "4,",
                },
            ),
            "done",
        ]
    )
    report = await evaluate(svc.s.profile(), 1)
    assert report["passed"] == 1 and report["cases"][0]["untouched"]
    replies.append("all done")
    assert (await evaluate(svc.s.profile(), 1))["passed"] == 0


async def test_mcp_reads_receipt_before_repeating_a_timed_out_write():
    calls = []

    class Client:
        async def call_tool(self, name, args):
            calls.append((name, args))
            if len(calls) == 1:
                raise TimeoutError
            return CallToolResult(
                content=[],
                structuredContent={"operation_id": "stable", "status": "succeeded"},
            )

    spec = SimpleNamespace(
        name="write",
        inputSchema={"type": "object", "properties": {"key": {"type": "string"}}},
    )
    items = {
        "write": ExternalTool(Client(), spec, "key", Receipt(tool="status", field="id"))
    }
    assert "succeeded" in await invoke(items, "write", {}, "stable")
    assert calls == [("write", {"key": "stable"}), ("status", {"id": "stable"})]


async def test_explicit_delegate_resumes_child_before_parent(harness):
    svc, replies, requests, _ = harness
    (svc.s.root / "target.txt").write_text("artifact")

    def interrupted(request):
        raise asyncio.CancelledError

    replies.extend(
        [
            (
                "delegate",
                {
                    "role": "Explorer",
                    "goal": "inspect target",
                    "acceptance": "report contents",
                },
            ),
            ("read_file", {"path": "target.txt"}),
            interrupted,
        ]
    )
    with pytest.raises(asyncio.CancelledError):
        await svc.submit("inspect using an explorer")
    svc.resume(svc.sid)
    before = len(requests)
    replies.extend(["contains artifact", "review complete"])
    assert await svc.submit("/continue") == "review complete"
    child, parent = requests[before:]
    assert any(
        m["role"] == "tool" and "artifact" in m["content"] for m in child["messages"]
    )
    assert any(
        m["role"] == "tool" and "contains artifact" in m["content"]
        for m in parent["messages"]
    )
